"""3.6: the database layer. Migrations match the models, the migrated MySQL schema
has the columns, character set, indexes and foreign key the application relies
on, defaults are stored as documented, stored file names fit their columns, and
deleting users, jobs or sessions behaves as docs/database-design.md describes.

Everything runs on Django's `test_` database and a temporary MEDIA_ROOT.
"""

import json
import uuid
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.generator.filenames import MAX_KEYWORD_LENGTH, upload_filename
from apps.generator.models import GenerationJob

from .base import MP4, PNG, TempMediaTestCase

S = GenerationJob.Status
TEXT_URL = '/api/v1/generations/text-to-video/'
FAST_HASHER = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


def schema(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, [connection.settings_dict['NAME'], *params])
        return cursor.fetchall()


class TestDatabaseGuard(TestCase):
    def test_runs_on_the_test_database(self):
        self.assertTrue(connection.settings_dict['NAME'].startswith('test_'), connection.settings_dict['NAME'])


class MigrationConsistencyTests(TestCase):
    def test_models_need_no_new_migration(self):
        # Exits with an error if any model change has no migration.
        call_command('makemigrations', check=True, dry_run=True, verbosity=0, stdout=StringIO())

    def test_every_migration_is_applied(self):
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_generator_has_only_the_initial_migration(self):
        executor = MigrationExecutor(connection)
        names = sorted(name for app, name in executor.loader.graph.nodes if app == 'generator')
        self.assertEqual(names, ['0001_initial'])


class SchemaTests(TestCase):
    """The schema the migration creates, read from MySQL's information_schema."""

    def columns(self):
        rows = schema('SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, CHARACTER_SET_NAME '
                      'FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s',
                      ['generation_jobs'])
        return {name: (kind, length, nullable == 'YES', charset) for name, kind, length, nullable, charset in rows}

    def test_table_is_innodb_utf8mb4(self):
        ((engine, collation),) = schema('SELECT ENGINE, TABLE_COLLATION FROM information_schema.TABLES '
                                         'WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s', ['generation_jobs'])
        self.assertEqual(engine, 'InnoDB')  # row locks, SKIP LOCKED and foreign keys need InnoDB
        self.assertTrue(collation.startswith('utf8mb4'), collation)  # emoji prompts need 4-byte UTF-8

    def test_columns_match_the_model(self):
        columns = self.columns()
        self.assertEqual(set(columns), {f.column for f in GenerationJob._meta.concrete_fields})
        for field in GenerationJob._meta.concrete_fields:
            kind, length, nullable, charset = columns[field.column]
            with self.subTest(field=field.name):
                self.assertEqual(nullable, field.null)
                if getattr(field, 'max_length', None) and kind == 'varchar':
                    self.assertEqual(length, field.max_length)
                if kind in ('varchar', 'char', 'longtext'):
                    self.assertEqual(charset, 'utf8mb4')
        self.assertEqual(columns['created_at'][0], 'datetime')
        self.assertEqual(columns['settings'][0], 'json')

    def test_media_is_referenced_by_name_never_stored(self):
        # Images and videos live on disk; the table holds only their names.
        kinds = {kind for kind, *_ in self.columns().values()}
        self.assertFalse(kinds & {'blob', 'tinyblob', 'mediumblob', 'longblob', 'binary', 'varbinary'}, kinds)
        self.assertEqual(self.columns()['input_file'][:2], ('varchar', 100))
        self.assertEqual(self.columns()['output_file'][:2], ('varchar', 100))

    def test_indexes_the_queries_rely_on(self):
        rows = schema('SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) '
                      'FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s '
                      'GROUP BY INDEX_NAME', ['generation_jobs'])
        indexes = {columns for _, columns in rows}
        self.assertIn('id', indexes)                       # detail, video, cancel
        self.assertIn('user_id,created_at', indexes)       # a user's history, newest first
        self.assertIn('session_key,created_at', indexes)   # a guest's history, newest first
        self.assertIn('status,created_at', indexes)        # claiming, recovery and queue expiry

    def test_user_foreign_key(self):
        rows = schema('SELECT k.COLUMN_NAME, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.DELETE_RULE '
                      'FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.REFERENTIAL_CONSTRAINTS r '
                      'USING (CONSTRAINT_SCHEMA, CONSTRAINT_NAME) WHERE k.TABLE_SCHEMA = %s AND k.TABLE_NAME = %s',
                      ['generation_jobs'])
        # Django creates the constraint without ON DELETE; CASCADE is done by the ORM.
        self.assertEqual(rows, (('user_id', 'auth_user', 'id', 'NO ACTION'),))


class ModelDefaultTests(TestCase):
    def test_a_new_job_stores_the_documented_defaults(self):
        before = timezone.now()
        job = GenerationJob.objects.create(mode=GenerationJob.Mode.TEXT_TO_VIDEO)
        job = GenerationJob.objects.get(pk=job.pk)
        self.assertIsInstance(job.id, uuid.UUID)
        self.assertEqual(job.id.version, 4)
        self.assertEqual(job.status, S.QUEUED)
        self.assertIsNone(job.user)
        self.assertEqual((job.session_key, job.prompt_text, job.remote_job_id, job.error_code,
                          job.error_message, job.request_id), ('',) * 6)
        self.assertEqual(job.settings, {})
        self.assertFalse(job.input_file)
        self.assertFalse(job.output_file)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.completed_at)
        self.assertFalse(job.video_available)
        self.assertTrue(before <= job.created_at <= timezone.now())
        self.assertEqual(job.created_at.utcoffset(), timedelta(0))  # stored and read as UTC

    def test_timestamps_keep_microseconds(self):
        # datetime(6): history ordering and lease comparisons are not rounded to seconds.
        moment = timezone.now().replace(microsecond=123456)
        job = GenerationJob.objects.create(mode=GenerationJob.Mode.TEXT_TO_VIDEO, created_at=moment)
        self.assertEqual(GenerationJob.objects.get(pk=job.pk).created_at, moment)

    def test_updated_at_changes_on_every_save(self):
        job = GenerationJob.objects.create(mode=GenerationJob.Mode.TEXT_TO_VIDEO)
        first = GenerationJob.objects.get(pk=job.pk).updated_at
        job.remote_job_id = 'remote-1'
        job.save(update_fields=['remote_job_id', 'updated_at'])
        self.assertGreater(GenerationJob.objects.get(pk=job.pk).updated_at, first)

    def test_settings_round_trip_as_json(self):
        settings = {'model_profile': 'wan2.1-t2v-1.3b-480p', 'width': 832, 'height': 480, 'frame_count': 81,
                    'fps': 16, 'seed': None, 'style': 'anime', 'enhance_prompt': False}
        job = GenerationJob.objects.create(mode=GenerationJob.Mode.TEXT_TO_VIDEO, settings=settings)
        stored = GenerationJob.objects.get(pk=job.pk).settings
        self.assertEqual(stored, settings)
        self.assertEqual(json.dumps(stored, sort_keys=True), json.dumps(settings, sort_keys=True))  # types kept

    def test_four_byte_characters_are_stored_in_every_text_column(self):
        text = 'Lighthouse 🌃 灯台'
        job = GenerationJob.objects.create(mode=GenerationJob.Mode.TEXT_TO_VIDEO, prompt_text=text,
                                           error_message=text)
        job = GenerationJob.objects.get(pk=job.pk)
        self.assertEqual((job.prompt_text, job.error_message), (text, text))


class StoredFileNameTests(TempMediaTestCase):
    """Names written to input_file and output_file always fit their varchar(100) columns."""

    def test_longest_input_name_fits(self):
        prompt = 'x' * 200  # keyword is cut to MAX_KEYWORD_LENGTH
        name = upload_filename(prompt, 'webp')
        self.assertEqual(len(name.split('-')[0]), MAX_KEYWORD_LENGTH)
        job = self.make_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        field = GenerationJob._meta.get_field('input_file')
        for _ in range(2):  # the second save gets storage's collision suffix
            job.input_file.save(name, ContentFile(PNG), save=True)
            stored = GenerationJob.objects.get(pk=job.pk).input_file.name
            self.assertLessEqual(len(stored), field.max_length)
            self.assertTrue(stored.startswith('inputs/'), stored)
            self.assertTrue((self.media_root / stored).exists())

    def test_output_name_fits(self):
        job = self.make_job()
        field = GenerationJob._meta.get_field('output_file')
        for _ in range(2):
            job.output_file.save(f'{job.id}.mp4', ContentFile(MP4), save=False)
            self.assertLessEqual(len(job.output_file.name), field.max_length)
            self.assertTrue(job.output_file.name.startswith('outputs/'))


@FAST_HASHER
class DeletionTests(TempMediaTestCase):
    def test_deleting_a_user_deletes_only_their_jobs_and_keeps_files(self):
        users = get_user_model().objects
        alice, bob = users.create_user('alice', password='unused'), users.create_user('bob', password='unused')
        alices = self.make_completed_job(user=alice)
        bobs = self.make_completed_job(user=bob)
        guests = self.make_job(session_key='guest-session-key')
        alice.delete()
        self.assertFalse(GenerationJob.objects.filter(pk=alices.pk).exists())
        self.assertEqual(set(GenerationJob.objects.values_list('pk', flat=True)), {bobs.pk, guests.pk})
        # Retention policy: Django never deletes files with their rows.
        self.assertTrue((self.media_root / alices.output_file.name).exists())

    def test_deleting_a_job_keeps_its_files(self):
        job = self.make_completed_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        job.input_file.save('cat-input.png', ContentFile(PNG), save=True)
        paths = [self.media_root / job.input_file.name, self.media_root / job.output_file.name]
        job.delete()
        self.assertTrue(all(path.exists() for path in paths))

    def test_mysql_refuses_a_raw_user_delete_while_jobs_reference_it(self):
        # The foreign key is NO ACTION in MySQL, so only the ORM's CASCADE removes jobs;
        # a raw DELETE cannot leave jobs pointing at a missing user.
        user = get_user_model().objects.create_user('carol', password='unused')
        job = self.make_job(user=user)
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('DELETE FROM auth_user WHERE id = %s', [user.pk])
        self.assertTrue(GenerationJob.objects.filter(pk=job.pk, user=user).exists())

    def test_deleting_a_guest_session_keeps_its_jobs_but_hides_them(self):
        response = self.client.post(TEXT_URL, {'prompt': 'A cat'}, content_type='application/json')
        self.assertEqual(response.status_code, 202)
        job_id = response.json()['job_id']
        session_key = self.session_key()
        self.assertEqual(GenerationJob.objects.get(pk=job_id).session_key, session_key)
        Session.objects.filter(session_key=session_key).delete()  # what clearsessions does to expired sessions
        self.assertTrue(GenerationJob.objects.filter(pk=job_id).exists())  # the row stays
        # The old cookie no longer reaches it: no history, and the job looks missing.
        self.assertEqual(self.client.get('/api/v1/generations/').json()['count'], 0)
        self.assertEqual(self.client.get(f'/api/v1/generations/{job_id}/').status_code, 404)
