"""3.5.6: find_orphaned_media, a report-only audit of MEDIA_ROOT.

Every test runs on the temporary MEDIA_ROOT from TempMediaMixin and the test
database. The command must never modify, move or delete anything.
"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.utils import timezone

from apps.generator.media_audit import audit_media
from apps.generator.models import GenerationJob

from .base import MP4, PNG, TempMediaTestCase

S = GenerationJob.Status
OLD = time.time() - 3 * 3600  # three hours ago: past the default 60-minute grace period
HEX = 'ab' * 16


class MediaAuditTestCase(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(os.path.realpath(settings.MEDIA_ROOT), os.path.realpath(self.media_root))

    def put(self, relative, content=b'x' * 10, age=OLD):
        path = self.media_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if age is not None:
            os.utime(path, (age, age))
        return path

    def run_command(self, *args):
        out = io.StringIO()
        call_command('find_orphaned_media', *args, stdout=out)
        return out.getvalue()

    def report_json(self, *args):
        return json.loads(self.run_command('--json', *args))

    def by_path(self, report):
        return {f.path: f for f in report.files}

    def snapshot(self):
        files = {}
        for path in sorted(self.media_root.rglob('*')):
            if path.is_file() and not path.is_symlink():
                info = path.stat()
                files[path.relative_to(self.media_root).as_posix()] = (
                    info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        rows = list(GenerationJob.objects.order_by('pk').values())
        return files, rows


class ClassificationTests(MediaAuditTestCase):
    def test_referenced_and_unreferenced_files(self):
        done = self.make_completed_job()  # output saved through storage
        image_job = self.make_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO, input_file=f'inputs/2026/10/cat-{HEX}.png')
        self.put(image_job.input_file.name, PNG)
        stray_input = f'inputs/2026/09/dog-{"cd" * 16}.png'
        self.put(stray_input, PNG)
        deleted_id = uuid.uuid4()
        self.put(f'outputs/2026/09/{deleted_id}.mp4', MP4)
        failed = self.make_job(status=S.FAILED, error_code='worker_interrupted')
        self.put(f'outputs/2026/10/{failed.id}.mp4', MP4)  # a discarded late result

        files = self.by_path(audit_media(show_names=True))
        self.assertEqual(files[done.output_file.name].category, 'referenced')
        self.assertEqual(files[image_job.input_file.name].category, 'referenced')
        self.assertEqual((files[stray_input].category, files[stray_input].reason),
                         ('orphan', 'input image that no job refers to'))
        deleted = files[f'outputs/2026/09/{deleted_id}.mp4']
        self.assertEqual((deleted.category, deleted.reason, deleted.job_id, deleted.job_status),
                         ('orphan', 'output of a job that no longer exists', str(deleted_id), 'deleted'))
        late = files[f'outputs/2026/10/{failed.id}.mp4']
        self.assertEqual((late.category, late.reason, late.job_status),
                         ('orphan', 'output of a failed job that does not refer to it', 'failed'))

    @override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
    def test_files_of_deleted_users_become_orphan_candidates(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user('alice', password='unused-test-password')
        job = self.make_completed_job(user=user, mode=GenerationJob.Mode.IMAGE_TO_VIDEO,
                                      input_file=f'inputs/2026/10/cat-{HEX}.png')
        self.put(job.input_file.name, PNG)
        os.utime(self.media_root / job.output_file.name, (OLD, OLD))
        self.assertEqual(audit_media().summary()['orphan']['count'], 0)
        user.delete()  # CASCADE deletes the job; Django never deletes its files
        files = self.by_path(audit_media())
        self.assertEqual(files[job.output_file.name].reason, 'output of a job that no longer exists')
        self.assertEqual(files[f'inputs/2026/10/*-{HEX}.png'].reason, 'input image that no job refers to')

    def test_nested_and_unexpected_locations(self):
        self.put('outputs/2025/01/deep/er/still/file.bin')
        self.put('loose-at-the-root.txt')
        self.put('other/folder/thing.dat')
        files = self.by_path(audit_media())
        self.assertEqual(files['outputs/2025/01/deep/er/still/file.bin'].reason, 'file in outputs/ that no job refers to')
        self.assertEqual(files['loose-at-the-root.txt'].reason, 'file outside inputs/ and outputs/ that nothing refers to')
        self.assertEqual(files['other/folder/thing.dat'].category, 'orphan')

    def test_placeholders_are_ignored(self):
        self.put('inputs/.gitkeep', b'')
        self.put('outputs/.gitkeep', b'')
        report = audit_media()
        self.assertEqual({f.path: f.category for f in report.files},
                         {'inputs/.gitkeep': 'ignored', 'outputs/.gitkeep': 'ignored'})
        self.assertEqual(report.summary()['orphan']['count'], 0)

    def test_missing_files_referenced_by_the_database(self):
        job = self.make_job(status=S.COMPLETED, output_file='outputs/2026/10/gone.mp4',
                            mode=GenerationJob.Mode.IMAGE_TO_VIDEO, input_file=f'inputs/2026/10/cat-{HEX}.png')
        report = audit_media()
        self.assertEqual(sorted((m['field'], m['path'], m['job_id'], m['status']) for m in report.missing), [
            ('input_file', f'inputs/2026/10/*-{HEX}.png', str(job.id), 'completed'),
            ('output_file', 'outputs/2026/10/gone.mp4', str(job.id), 'completed'),
        ])
        self.assertEqual(report.files, [])

    def test_references_outside_the_media_root_are_reported_not_checked(self):
        for name in ('../../escape.png', '/etc/passwd', 'C:/Windows/win.ini'):
            self.make_job(input_file=name)
        report = audit_media(show_names=True)
        self.assertEqual(sorted(entry['path'] for entry in report.outside_root),
                         sorted(['../../escape.png', '/etc/passwd', 'C:/Windows/win.ini']))
        self.assertEqual(report.missing, [])


class InUseTests(MediaAuditTestCase):
    def test_output_of_an_active_job_is_possibly_in_use_whatever_its_age(self):
        for status in (S.PROCESSING, S.QUEUED):
            with self.subTest(status=status):
                job = self.make_job(status=status)
                for name in (f'{job.id}.mp4', f'{job.id}_Ab12Cd3.mp4'):  # storage may add a suffix
                    self.put(f'outputs/2026/10/{name}', MP4)
                files = self.by_path(audit_media())
                for name in (f'{job.id}.mp4', f'{job.id}_Ab12Cd3.mp4'):
                    item = files[f'outputs/2026/10/{name}']
                    self.assertEqual((item.category, item.job_status), ('possibly_in_use', status))
                    self.assertIn('may be recorded soon', item.reason)

    def test_recent_unreferenced_files_are_possibly_in_use(self):
        self.put(f'inputs/2026/10/new-{HEX}.png', PNG, age=None)  # just written, like an upload in progress
        self.put('outputs/2026/10/just-now.mp4', MP4, age=time.time() - 59 * 60)
        report = audit_media()
        self.assertEqual({f.category for f in report.files}, {'possibly_in_use'})
        self.assertEqual(report.summary()['orphan']['count'], 0)
        self.assertTrue(all('within the last 60 minutes' in f.reason for f in report.files))

    def test_grace_period_is_configurable(self):
        self.put('outputs/2026/10/ten-minutes.mp4', MP4, age=time.time() - 600)
        self.assertEqual(audit_media(grace_minutes=60).files[0].category, 'possibly_in_use')
        self.assertEqual(audit_media(grace_minutes=5).files[0].category, 'orphan')
        self.assertEqual(self.report_json('--grace-minutes', '0')['summary']['orphan']['count'], 1)

    def test_worker_temporary_files_are_not_in_media(self):
        # The worker renders into a system temporary folder and only then saves the
        # finished video into outputs/; nothing under MEDIA_ROOT is its scratch space.
        job = self.make_job(status=S.PROCESSING)
        scratch = Path(tempfile.mkdtemp(prefix='geetha-test-worker-'))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        (scratch / f'{job.id}.mp4').write_bytes(MP4)
        self.assertFalse(scratch.is_relative_to(self.media_root))
        self.assertEqual(audit_media().files, [])


class SafetyTests(MediaAuditTestCase):
    def populate(self):
        self.make_completed_job()
        self.put(f'inputs/2026/09/dog-{"cd" * 16}.png', PNG)
        self.put(f'outputs/2026/09/{uuid.uuid4()}.mp4', MP4)
        self.put('outputs/2026/10/recent.mp4', MP4, age=None)
        self.make_job(status=S.PROCESSING)
        self.make_job(status=S.COMPLETED, output_file='outputs/2026/10/gone.mp4')

    def test_never_modifies_files_or_database_rows(self):
        self.populate()
        before = self.snapshot()
        forbidden = mock.Mock(side_effect=AssertionError('the audit must not change anything'))
        with mock.patch('os.remove', forbidden), mock.patch('os.unlink', forbidden), \
                mock.patch('os.rename', forbidden), mock.patch('os.replace', forbidden), \
                mock.patch('os.rmdir', forbidden), mock.patch('shutil.rmtree', forbidden), \
                mock.patch('shutil.move', forbidden), mock.patch.object(Path, 'unlink', forbidden), \
                mock.patch.object(Path, 'rename', forbidden), mock.patch.object(Path, 'write_bytes', forbidden), \
                mock.patch.object(default_storage, 'delete', forbidden), \
                mock.patch.object(default_storage, 'save', forbidden), \
                mock.patch.object(GenerationJob, 'save', forbidden), mock.patch.object(GenerationJob, 'delete', forbidden):
            for args in ((), ('--json',), ('--show-file-names',), ('--grace-minutes', '0')):
                self.run_command(*args)
        forbidden.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_files_are_never_opened(self):
        self.populate()
        real_open = open
        opened = []

        def watching_open(file, *args, **kwargs):
            if str(file).startswith(str(self.media_root)):
                opened.append(file)
            return real_open(file, *args, **kwargs)

        with mock.patch('builtins.open', watching_open):
            audit_media()
        self.assertEqual(opened, [])

    def test_symlinks_are_not_followed(self):
        outside = Path(tempfile.mkdtemp(prefix='geetha-test-outside-'))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / 'secret.mp4').write_bytes(MP4)
        try:
            os.symlink(outside, self.media_root / 'linked-dir', target_is_directory=True)
            os.symlink(outside / 'secret.mp4', self.media_root / 'linked-file.mp4')
        except (OSError, NotImplementedError):
            self.skipTest('creating symbolic links needs extra rights on this system')
        report = audit_media()
        self.assertEqual(sorted(report.links), ['linked-dir', 'linked-file.mp4'])
        self.assertEqual(report.files, [])
        self.assertTrue((outside / 'secret.mp4').exists())

    def test_link_detection_prunes_directories_and_skips_files(self):
        # Runs everywhere (creating real symlinks on Windows needs extra rights).
        self.put('outputs/linked/inside.mp4', MP4)
        self.put('outputs/pointer.mp4', MP4)
        self.put('outputs/2026/10/plain.mp4', MP4)
        root = os.path.realpath(self.media_root)  # the audit works on the resolved root
        links = {os.path.join(root, 'outputs', 'linked'), os.path.join(root, 'outputs', 'pointer.mp4')}
        with mock.patch('apps.generator.media_audit._is_link', side_effect=lambda path: path in links):
            report = audit_media()
        self.assertEqual(sorted(report.links), ['outputs/linked', 'outputs/pointer.mp4'])
        self.assertEqual([f.path for f in report.files], ['outputs/2026/10/plain.mp4'])

    def test_junctions_are_not_followed(self):
        try:
            import _winapi
        except ImportError:
            self.skipTest('directory junctions exist only on Windows')
        outside = Path(tempfile.mkdtemp(prefix='geetha-test-outside-'))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / 'secret.mp4').write_bytes(MP4)
        junction = self.media_root / 'outputs' / 'junction'
        junction.parent.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(outside), str(junction))
        self.addCleanup(os.rmdir, junction)  # removes the junction only, never its target
        report = audit_media()
        self.assertEqual(report.links, ['outputs/junction'])
        self.assertEqual(report.files, [])
        self.assertTrue((outside / 'secret.mp4').exists())

    def test_missing_media_root(self):
        with override_settings(MEDIA_ROOT=str(self.media_root / 'does-not-exist')), \
                self.assertRaisesMessage(CommandError, 'MEDIA_ROOT does not exist'):
            self.run_command()

    def test_unsafe_media_roots_are_refused_before_scanning(self):
        # A misconfigured MEDIA_ROOT (for example MEDIA_ROOT= left empty in .env,
        # which makes it the project folder) would list source files as orphans.
        project_root = Path(settings.BASE_DIR).parent
        drive_root = Path(os.path.abspath(os.sep))
        for root, message in ((drive_root, 'root of a drive or file system'),
                              (project_root, 'contains application files'),
                              (Path(settings.BASE_DIR), 'contains application files'),
                              (Path(settings.FRONTEND_DIR), 'contains application files')):
            with self.subTest(root=str(root)), override_settings(MEDIA_ROOT=str(root)), \
                    mock.patch('apps.generator.media_audit.os.walk') as walk:
                with self.assertRaisesMessage(CommandError, message):
                    self.run_command()
                with self.assertRaisesMessage(CommandError, message):
                    self.run_command('--json')
                walk.assert_not_called()

    def test_ordinary_media_folder_is_accepted(self):
        # Like the default <project>/media: beside the code, not containing it.
        nested = self.media_root / 'media'
        nested.mkdir()
        with override_settings(MEDIA_ROOT=str(nested)):
            self.assertIn('report only', self.run_command())

    def test_negative_grace_period_is_refused(self):
        with self.assertRaisesMessage(CommandError, '--grace-minutes must be 0 or more'):
            self.run_command('--grace-minutes', '-1')


class OutputTests(MediaAuditTestCase):
    def setUp(self):
        super().setUp()
        self.session = 's' * 32
        self.make_job(self.session, prompt_text='SECRETPROMPT white cat', mode=GenerationJob.Mode.IMAGE_TO_VIDEO,
                      input_file=f'inputs/2026/10/white-cat-{HEX}.png', request_id='req-secret')
        self.put(f'inputs/2026/10/white-cat-{HEX}.png', PNG)
        self.put(f'inputs/2026/09/private-diary-{"cd" * 16}.png', PNG)
        self.job = self.make_job(self.session, status=S.COMPLETED, output_file='outputs/2026/10/gone.mp4')

    def test_prompts_and_session_keys_never_appear(self):
        for args in ((), ('--json',), ('--show-file-names',)):
            with self.subTest(args=args):
                text = self.run_command(*args)
                for secret in ('SECRETPROMPT', self.session, 'req-secret'):
                    self.assertNotIn(secret, text)

    def test_prompt_keywords_in_file_names_are_masked_by_default(self):
        text = self.run_command()
        self.assertIn(f'inputs/2026/09/*-{"cd" * 16}.png', text)
        self.assertNotIn('private-diary', text)
        self.assertIn('private-diary', self.run_command('--show-file-names'))

    def test_text_report(self):
        text = self.run_command()
        self.assertIn('(report only: nothing was changed)', text)
        self.assertIn('Orphan candidates:', text)
        self.assertRegex(text, r'Orphan candidates:\s+1 files')
        self.assertRegex(text, r'Referenced by the database:\s+1 files')
        self.assertRegex(text, r'Database references to missing files:\s+1')
        self.assertIn('review manually before removing anything', text)
        self.assertIn(f'outputs/2026/10/gone.mp4  generator.GenerationJob.output_file  job {self.job.id} (completed)', text)

    def test_json_report(self):
        data = self.report_json()
        self.assertEqual(data['media_root'], os.path.realpath(self.media_root))
        self.assertEqual(data['summary']['orphan'], {'count': 1, 'bytes': len(PNG)})
        self.assertEqual(data['summary']['referenced'], {'count': 1, 'bytes': len(PNG)})
        self.assertEqual([f['path'] for f in data['files']], [f'inputs/2026/09/*-{"cd" * 16}.png'])
        self.assertEqual(data['missing_files'][0]['job_id'], str(self.job.id))
        self.assertEqual((data['references_outside_media_root'], data['skipped_links']), ([], []))
        self.assertTrue(data['files'][0]['modified'].endswith('+00:00'))  # UTC

    def test_empty_media_root(self):
        GenerationJob.objects.all().delete()
        for path in self.media_root.rglob('*'):
            if path.is_file():
                path.unlink()
        data = self.report_json()
        self.assertEqual({key: value['count'] for key, value in data['summary'].items()},
                         {'referenced': 0, 'orphan': 0, 'possibly_in_use': 0, 'ignored': 0})
