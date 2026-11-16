"""3.7: media files on disk and the database references to them.

- A write that fails part-way (for example a full disk) leaves nothing behind,
  for uploads and for generated videos; existing files are never overwritten
  or removed by someone else's cleanup.
- An image stored for a job whose row cannot be inserted is removed again.
- Stored references are only used while they resolve to a regular file inside
  MEDIA_ROOT: traversal names, absolute paths, directories and links (symlinks
  or junctions) that lead outside are treated as missing, by the video endpoint
  and by the worker, so neither ever reads a file outside MEDIA_ROOT.
- Temporary upload files are removed after the request, accepted or rejected.
- An expired guest session no longer reaches its jobs or videos.

Every test uses the temporary MEDIA_ROOT and the test database. Files "outside
MEDIA_ROOT" are in a separate temporary folder.
"""

import errno
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.sessions.models import Session
from django.core.files.base import ContentFile, File
from django.core.files.uploadedfile import InMemoryUploadedFile, SimpleUploadedFile
from django.db import OperationalError
from django.test import override_settings
from django.utils import timezone

from apps.generator.media_files import stored_file_path
from apps.generator.models import GenerationJob
from apps.generator.worker import process_job

from .base import MP4, PNG, TempMediaTestCase, TempMediaTransactionTestCase
from .test_worker import FakeClient

S = GenerationJob.Status
IMAGE_URL = '/api/v1/generations/image-to-video/'
SECRET = b'\x00\x00\x00\x18ftypisom' + b'OUTSIDE-SECRET' * 100


def failing_chunks(self, chunk_size=None):
    """Writes part of the file, then fails like a full disk."""
    self.seek(0)
    yield self.read(100)
    raise OSError(errno.ENOSPC, 'No space left on device')


def make_junction(target, link):
    try:
        import _winapi
    except ImportError:
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(link))
    return True


class MediaStorageMixin:
    def outside_dir(self):
        outside = Path(tempfile.mkdtemp(prefix='geetha-test-outside-'))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        return outside

    def link_dir(self, target, link):
        """A directory link (junction on Windows, else symlink); removed before the media folder."""
        if not make_junction(target, link):
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('this system cannot create directory links')
        self.addCleanup(os.rmdir if os.name == 'nt' else os.unlink, link)  # the link only, never its target

    def files_under(self, folder=''):
        root = self.media_root / folder
        return sorted(p.relative_to(self.media_root).as_posix() for p in root.rglob('*') if p.is_file())

    def processing_job(self, **fields):
        job = self.make_job(**fields)
        job.transition_to(S.PROCESSING)
        return job

    def completed_job_referencing(self, name):
        return self.make_job(self.session_key(), status=S.COMPLETED, remote_job_id='mock-x',
                             completed_at=timezone.now(), output_file=name)

    def get_video(self, job, **params):
        response = self.client.get(f'/api/v1/generations/{job.id}/video/', params)
        response.body = b''.join(response.streaming_content) if response.streaming else response.content
        return response


class MediaStorageTestCase(MediaStorageMixin, TempMediaTestCase):
    pass


class UploadCleanupTests(MediaStorageMixin, TempMediaTransactionTestCase):
    # Committed rows, as in production (autocommit): inside TestCase's transaction a
    # failed INSERT would mark the whole test transaction for rollback.

    def post_image(self):
        image = SimpleUploadedFile('photo.png', PNG, content_type='image/png')
        return self.client.post(IMAGE_URL, {'image': image, 'prompt': 'A white cat'})

    def test_partly_written_upload_is_removed(self):
        with mock.patch.object(InMemoryUploadedFile, 'chunks', failing_chunks), \
                self.assertLogs('apps.generator.exceptions', level='ERROR'):
            response = self.post_image()
        error = self.assertApiError(response, 500, 'internal_error')
        self.assertNotIn('space', error['message'])
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertEqual(self.files_under(), [])

    def test_image_is_removed_when_the_job_cannot_be_inserted(self):
        with mock.patch.object(GenerationJob, '_do_insert', side_effect=OperationalError(2006, 'gone away')), \
                self.assertLogs('apps.generator.exceptions', level='ERROR'):
            response = self.post_image()
        self.assertApiError(response, 500, 'internal_error')
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertEqual(self.files_under(), [])

    def test_image_is_kept_when_the_insert_may_have_committed(self):
        # The row is written, then the connection drops before the reply arrives.
        real_insert = GenerationJob._do_insert

        def insert_then_lose_the_reply(job, *args, **kwargs):
            real_insert(job, *args, **kwargs)
            raise OperationalError(2013, 'Lost connection to server during query')

        with mock.patch.object(GenerationJob, '_do_insert', insert_then_lose_the_reply), \
                self.assertLogs('apps.generator.exceptions', level='ERROR'):
            response = self.post_image()
        self.assertApiError(response, 500, 'internal_error')
        job = GenerationJob.objects.get()
        self.assertEqual(self.files_under(), [job.input_file.name])
        self.assertEqual((self.media_root / job.input_file.name).read_bytes(), PNG)

    def test_image_is_kept_when_the_recheck_fails_too(self):
        # Insert outcome unknown and the confirming read fails: nothing proves the file unused.
        from django.db.models.query import QuerySet
        self.session_key()  # start the guest session first: creating one also uses exists()
        with mock.patch.object(GenerationJob, '_do_insert', side_effect=OperationalError(2013, 'Lost connection')) as insert,                 mock.patch.object(QuerySet, 'exists', side_effect=OperationalError(2006, 'gone away')),                 self.assertLogs('apps.generator.exceptions', level='ERROR'):
            response = self.post_image()
        self.assertApiError(response, 500, 'internal_error')
        insert.assert_called_once()  # the failure happened at the INSERT, after the image was stored
        stored = self.files_under('inputs')
        self.assertEqual(len(stored), 1)
        self.assertEqual((self.media_root / stored[0]).read_bytes(), PNG)

    def test_stored_upload_still_belongs_to_its_job(self):
        response = self.post_image()
        self.assertEqual(response.status_code, 202, response.content)
        job = GenerationJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual(self.files_under(), [job.input_file.name])
        self.assertRegex(job.input_file.name, r'^inputs/\d{4}/\d{2}/white-cat-[0-9a-f]{32}\.png$')
        self.assertEqual((self.media_root / job.input_file.name).read_bytes(), PNG)

    def test_temporary_upload_files_are_removed_after_the_request(self):
        upload_tmp = Path(tempfile.mkdtemp(prefix='geetha-test-upload-tmp-'))
        self.addCleanup(shutil.rmtree, upload_tmp, ignore_errors=True)
        # A 1 KB memory limit makes Django spool even the small test image to disk.
        with override_settings(FILE_UPLOAD_TEMP_DIR=str(upload_tmp), FILE_UPLOAD_MAX_MEMORY_SIZE=1):
            accepted = self.post_image()
            rejected = self.client.post(IMAGE_URL, {'image': SimpleUploadedFile('x.png', b'not an image' * 200)})
        self.assertEqual(accepted.status_code, 202, accepted.content)
        self.assertApiError(rejected, 400, 'unsupported_image_type', fields=['image'])
        self.assertEqual(list(upload_tmp.iterdir()), [])
        job = GenerationJob.objects.get()
        self.assertEqual(self.files_under(), [job.input_file.name])
        self.assertEqual((self.media_root / job.input_file.name).read_bytes(), PNG)


class OutputStorageTests(MediaStorageTestCase):
    def expected_name(self, job):
        name = GenerationJob._meta.get_field('output_file').generate_filename(job, f'{job.id}.mp4')
        return name.replace('\\', '/')  # storage stores '/'-separated names

    def test_partly_written_video_is_removed_and_the_job_fails(self):
        job = self.processing_job()
        with mock.patch.object(File, 'chunks', failing_chunks), self.assertLogs('apps.generator.worker', 'ERROR'):
            process_job(job, client=FakeClient())
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'internal_error'))
        self.assertFalse(job.output_file)
        self.assertFalse(job.video_available)
        self.assertEqual(self.files_under(), [])

    def test_existing_file_with_the_same_name_is_never_overwritten(self):
        job = self.processing_job()
        existing = self.media_root / self.expected_name(job)
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'kept by an operator')
        process_job(job, client=FakeClient())
        job.refresh_from_db()
        self.assertEqual(job.status, S.COMPLETED)
        self.assertNotEqual(job.output_file.name, self.expected_name(job))
        self.assertEqual((self.media_root / job.output_file.name).read_bytes(), MP4)
        self.assertEqual(existing.read_bytes(), b'kept by an operator')

    def test_cleanup_after_a_failed_write_keeps_other_files(self):
        job = self.processing_job()
        existing = self.media_root / self.expected_name(job)
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'kept by an operator')
        other = self.make_completed_job()
        with mock.patch.object(File, 'chunks', failing_chunks), self.assertLogs('apps.generator.worker', 'ERROR'):
            process_job(job, client=FakeClient())
        job.refresh_from_db()
        self.assertEqual(job.status, S.FAILED)
        self.assertEqual(existing.read_bytes(), b'kept by an operator')
        kept = existing.relative_to(self.media_root).as_posix()  # not recomputed: the month may have changed
        self.assertEqual(self.files_under(), sorted([kept, other.output_file.name]))

    def test_recorded_reference_matches_the_stored_file(self):
        job = self.processing_job()
        process_job(job, client=FakeClient())
        job.refresh_from_db()
        self.assertEqual(self.files_under(), [job.output_file.name])
        self.assertRegex(job.output_file.name, rf'^outputs/\d{{4}}/\d{{2}}/{job.id}\.mp4$')
        self.assertEqual(stored_file_path(job.output_file), Path(os.path.realpath(self.media_root / job.output_file.name)))


class DiscardFailureTests(MediaStorageTestCase):
    def test_undeletable_discarded_output_is_logged_and_the_worker_carries_on(self):
        job = self.processing_job()

        class DeletingClient(FakeClient):
            def generate(client, request, output_path, on_submitted=None):
                GenerationJob.objects.filter(pk=job.pk).delete()  # deleted while generating
                super().generate(request, output_path, on_submitted)

        storage = GenerationJob._meta.get_field('output_file').storage
        with mock.patch.object(storage, 'delete', side_effect=PermissionError(13, 'denied')),                 self.assertLogs('apps.generator.worker', level='ERROR') as logs:
            process_job(job, client=DeletingClient())  # must not raise
        self.assertTrue(any('could not delete discarded output' in line for line in logs.output), logs.output)
        self.assertFalse(GenerationJob.objects.filter(pk=job.pk).exists())
        self.assertEqual(len(self.files_under('outputs')), 1)  # left for the media audit


class VideoReferenceSafetyTests(MediaStorageTestCase):
    def assertNotServed(self, job):
        response = self.get_video(job)
        self.assertNotIn(b'OUTSIDE-SECRET', response.body)
        self.assertEqual(response.status_code, 404)
        error = self.assertApiError(response, 404, 'not_found')
        self.assertNotIn(str(self.media_root), error['message'])
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_traversal_and_absolute_references_are_not_found(self):
        outside = self.outside_dir()
        (outside / 'secret.mp4').write_bytes(SECRET)
        for name in ('../secret.mp4', 'outputs/../../secret.mp4', str(outside / 'secret.mp4'),
                     (outside / 'secret.mp4').as_posix()):
            with self.subTest(name=name):
                self.assertNotServed(self.completed_job_referencing(name))

    def test_reference_to_a_directory_is_not_found(self):
        (self.media_root / 'outputs' / '2026').mkdir(parents=True)
        for name in ('outputs/2026', 'outputs', '.'):
            with self.subTest(name=name):
                self.assertNotServed(self.completed_job_referencing(name))

    def test_directory_link_leading_outside_is_not_followed(self):
        outside = self.outside_dir()
        (outside / 'secret.mp4').write_bytes(SECRET)
        self.link_dir(outside, self.media_root / 'outputs' / 'linked')
        job = self.completed_job_referencing('outputs/linked/secret.mp4')
        self.assertNotServed(job)
        self.assertEqual((outside / 'secret.mp4').read_bytes(), SECRET)

    def test_file_symlink_leading_outside_is_not_followed(self):
        outside = self.outside_dir()
        (outside / 'secret.mp4').write_bytes(SECRET)
        link = self.media_root / 'outputs' / 'secret.mp4'
        link.parent.mkdir(parents=True)
        try:
            os.symlink(outside / 'secret.mp4', link)
        except (OSError, NotImplementedError):
            self.skipTest('creating symbolic links needs extra rights on this system')
        self.assertNotServed(self.completed_job_referencing('outputs/secret.mp4'))

    def test_file_link_leading_outside_is_not_followed_simulated(self):
        # Runs everywhere (creating real symlinks on Windows needs extra rights): the
        # stored file "resolves" to a file outside MEDIA_ROOT, as a symlink would.
        job = self.make_completed_job(self.session_key())
        outside = self.outside_dir()
        (outside / 'secret.mp4').write_bytes(SECRET)
        linked = os.path.realpath(self.media_root / job.output_file.name)
        real_realpath = os.path.realpath
        with mock.patch('apps.generator.media_files.os.path.realpath',
                        side_effect=lambda p: str(outside / 'secret.mp4') if real_realpath(p) == linked
                        else real_realpath(p)):
            self.assertNotServed(job)
        self.assertEqual(self.get_video(job).status_code, 200)  # unaffected without the link

    def test_links_that_stay_inside_media_root_still_work(self):
        job = self.make_completed_job(self.session_key())
        self.link_dir(self.media_root / 'outputs', self.media_root / 'alias')
        aliased = self.completed_job_referencing('alias/' + job.output_file.name.split('/', 1)[1])
        for each in (job, aliased):
            response = self.get_video(each)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.body, MP4)

    def test_media_root_reached_through_a_link_still_works(self):
        job = self.make_completed_job(self.session_key())
        holder = self.outside_dir()
        self.link_dir(self.media_root, holder / 'media-link')
        with override_settings(MEDIA_ROOT=str(holder / 'media-link')):
            job.refresh_from_db()
            response = self.get_video(job, download='1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, MP4)

    def test_unreadable_video_is_a_generic_error_without_paths(self):
        job = self.make_completed_job(self.session_key())
        denied = PermissionError(errno.EACCES, 'Permission denied', str(self.media_root / job.output_file.name))
        with mock.patch('django.db.models.fields.files.FieldFile.open', side_effect=denied), \
                self.assertLogs('apps.generator.exceptions', level='ERROR'):
            response = self.get_video(job)
        error = self.assertApiError(response, 500, 'internal_error')
        self.assertNotIn(str(self.media_root), response.content.decode())
        self.assertNotIn('outputs', error['message'])
        self.assertEqual(response['Cache-Control'], 'private, no-store')


class InputReferenceSafetyTests(MediaStorageTestCase):
    def assertInputMissing(self, job):
        client = FakeClient()
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            process_job(job, client=client)
        job.refresh_from_db()
        self.assertIsNone(client.request)  # the AI service was never contacted
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'input_missing'))
        self.assertFalse(job.output_file)

    def image_job(self, name):
        return self.processing_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO, input_file=name)

    def test_traversal_absolute_and_directory_references(self):
        outside = self.outside_dir()
        (outside / 'secret.png').write_bytes(PNG)
        (self.media_root / 'inputs' / '2026').mkdir(parents=True)
        for name in ('../secret.png', str(outside / 'secret.png'), 'inputs/2026', 'inputs'):
            with self.subTest(name=name):
                self.assertInputMissing(self.image_job(name))

    def test_directory_link_leading_outside_is_not_followed(self):
        outside = self.outside_dir()
        (outside / 'secret.png').write_bytes(PNG)
        self.link_dir(outside, self.media_root / 'inputs' / 'linked')
        self.assertInputMissing(self.image_job('inputs/linked/secret.png'))
        self.assertEqual((outside / 'secret.png').read_bytes(), PNG)

    def test_valid_input_is_still_passed_to_the_client(self):
        job = self.make_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        job.input_file.save('cat-' + 'ab' * 16 + '.png', ContentFile(PNG), save=True)
        job.transition_to(S.PROCESSING)
        client = FakeClient()
        process_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, S.COMPLETED)
        self.assertEqual(client.request.image_path, Path(job.input_file.path))
        self.assertEqual(client.request.image_path.read_bytes(), PNG)


class ExpiredSessionTests(MediaStorageTestCase):
    def test_expired_guest_session_no_longer_reaches_its_jobs(self):
        key = self.session_key()
        job = self.make_completed_job(key)
        detail = f'/api/v1/generations/{job.id}/'
        self.assertEqual(self.get_video(job).status_code, 200)
        Session.objects.filter(session_key=key).update(expire_date=timezone.now() - timedelta(seconds=1))
        self.assertApiError(self.get_video(job), 404, 'not_found')
        self.assertApiError(self.client.get(detail), 404, 'not_found')
        self.assertEqual(self.client.get('/api/v1/generations/').json()['results'], [])
        # Nothing was removed: the job and its video stay for review (retention policy).
        job.refresh_from_db()
        self.assertTrue(job.video_available)
        self.assertTrue((self.media_root / job.output_file.name).is_file())


class LogPrivacyTests(MediaStorageTestCase):
    def test_cleanup_logs_never_contain_the_prompt_keyword(self):
        from apps.generator.media_files import loggable_name
        hex_ = 'ab' * 16
        self.assertEqual(loggable_name(f'inputs/2026/10/white-cat-{hex_}.png'), f'inputs/2026/10/*-{hex_}.png')
        windows_name = chr(92).join(['inputs', '2026', '10', f'secret-{hex_}.jpg'])  # backslash-separated
        self.assertEqual(loggable_name(windows_name), f'inputs/2026/10/*-{hex_}.jpg')
        job_output = 'outputs/2026/10/6531f3b2-8a14-4f78-a645-59254ab8b02d.mp4'
        self.assertEqual(loggable_name(job_output), job_output)  # job IDs are not prompt text

    def test_partial_upload_cleanup_is_logged_without_the_keyword(self):
        image = SimpleUploadedFile('photo.png', PNG, content_type='image/png')
        with mock.patch.object(InMemoryUploadedFile, 'chunks', failing_chunks), \
                self.assertLogs('apps.generator', level='INFO') as logs:
            self.client.post(IMAGE_URL, {'image': image, 'prompt': 'Privatekeyword garden'})
        self.assertTrue(any('partly written file inputs/' in line for line in logs.output), logs.output)
        self.assertFalse(any('privatekeyword' in line.lower() for line in logs.output), logs.output)
