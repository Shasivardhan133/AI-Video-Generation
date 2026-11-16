"""Background worker and the mock AI client."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.files.base import ContentFile
from django.utils import timezone

from ai_service.client import AIServiceError
from ai_service.client.base import verify_mp4
from ai_service.client.factory import get_client
from ai_service.client.mock import MockClient, find_ffmpeg
from ai_service.client.remote import RemoteClient
from apps.generator import worker
from apps.generator.models import GenerationJob
from apps.generator.worker import build_prompt, claim_next_job, process_job, recover_interrupted_jobs

from .base import IMAGE_SETTINGS, MP4, PNG, TEXT_SETTINGS, TempMediaTestCase

S = GenerationJob.Status
HAS_FFMPEG = find_ffmpeg() is not None


class FakeClient:
    """Stands in for an AI client: records the request, then writes or raises."""

    def __init__(self, write=MP4, raises=None, remote_id='fake-1'):
        self.write, self.raises, self.remote_id = write, raises, remote_id
        self.request = None

    def generate(self, request, output_path, on_submitted=None):
        self.request = request
        if on_submitted:
            on_submitted(self.remote_id)
        if self.raises:
            raise self.raises
        Path(output_path).write_bytes(self.write)


class BuildPromptTests(TempMediaTestCase):
    def test_style_hint_is_appended(self):
        job = self.make_job(prompt_text='  A cat  ', settings={'style': 'anime'})
        self.assertEqual(build_prompt(job), 'A cat, anime style')

    def test_style_hint_alone_when_prompt_empty(self):
        job = self.make_job(prompt_text='', settings={'style': 'cinematic'})
        self.assertEqual(build_prompt(job), 'cinematic lighting, film look')

    def test_no_style(self):
        self.assertEqual(build_prompt(self.make_job(prompt_text='A cat')), 'A cat')


class ClaimAndRecoverTests(TempMediaTestCase):
    def test_claims_oldest_queued_job(self):
        self.make_job(minutes_ago=1)
        oldest = self.make_job(minutes_ago=5)
        self.make_job(minutes_ago=9, status=S.FAILED)
        claimed = claim_next_job()
        self.assertEqual(claimed.pk, oldest.pk)
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, S.PROCESSING)
        self.assertIsNotNone(claimed.started_at)

    def test_returns_none_when_queue_empty(self):
        self.make_job(status=S.COMPLETED)
        self.assertIsNone(claim_next_job())

    def test_recover_fails_only_stale_processing_jobs(self):
        stuck = self.make_job(status=S.PROCESSING)
        GenerationJob.objects.filter(pk=stuck.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        active = self.make_job(status=S.PROCESSING)  # fresh heartbeat: another worker's job
        queued = self.make_job()
        self.assertEqual(recover_interrupted_jobs(), 1)
        for job in (stuck, active, queued):
            job.refresh_from_db()
        self.assertEqual(stuck.status, S.FAILED)
        self.assertEqual(stuck.error_code, 'worker_interrupted')
        self.assertEqual(active.status, S.PROCESSING)
        self.assertEqual(queued.status, S.QUEUED)


class ProcessJobTests(TempMediaTestCase):
    def processing_job(self, **fields):
        job = self.make_job(**fields)
        job.transition_to(S.PROCESSING)
        return job

    def test_success_stores_output_and_completes(self):
        job = self.processing_job(prompt_text='A cat', settings={**TEXT_SETTINGS, 'seed': 7, 'style': 'anime'})
        client = FakeClient(remote_id='r' * 150)
        process_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, S.COMPLETED)
        self.assertEqual(job.remote_job_id, 'r' * 100)
        self.assertTrue(job.output_file.name.startswith('outputs/'))
        self.assertEqual((self.media_root / job.output_file.name).read_bytes(), MP4)
        request = client.request
        self.assertEqual(request.prompt, 'A cat, anime style')
        self.assertEqual((request.width, request.height, request.frame_count, request.fps, request.seed),
                         (832, 480, 81, 16, 7))
        self.assertEqual(request.wan_task, 't2v-1.3B')
        self.assertIsNone(request.image_path)

    def test_image_job_passes_image_path(self):
        job = self.make_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        job.input_file.save('in.png', ContentFile(PNG), save=True)
        job.transition_to(S.PROCESSING)
        client = FakeClient()
        process_job(job, client=client)
        self.assertEqual(client.request.image_path, Path(job.input_file.path))
        self.assertEqual(client.request.wan_task, 'i2v-14B')

    def test_ai_service_error_fails_job_with_its_code(self):
        job = self.processing_job()
        process_job(job, client=FakeClient(raises=AIServiceError('service_timeout', 'The video took too long to generate.')))
        job.refresh_from_db()
        self.assertEqual(job.status, S.FAILED)
        self.assertEqual(job.error_code, 'service_timeout')
        self.assertEqual(job.error_message, 'The video took too long to generate.')
        self.assertFalse(job.output_file)
        self.assertFalse((self.media_root / 'outputs').exists())

    def test_unexpected_error_fails_job_with_generic_message(self):
        job = self.processing_job()
        with self.assertLogs('apps.generator.worker', level='ERROR'):
            process_job(job, client=FakeClient(raises=RuntimeError('secret internal detail')))
        job.refresh_from_db()
        self.assertEqual(job.status, S.FAILED)
        self.assertEqual(job.error_code, 'internal_error')
        self.assertNotIn('secret', job.error_message)

    def test_unknown_profile_fails_without_calling_client(self):
        job = self.processing_job(settings={'model_profile': 'retired-profile'})
        client = FakeClient()
        process_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, S.FAILED)
        self.assertEqual(job.error_code, 'unsupported_setting')
        self.assertIsNone(client.request)


class JobPreparationTests(TempMediaTestCase):
    """3.5.1: a job whose stored settings or input cannot become an AI request fails
    at once, without calling the AI client, instead of staying `processing` until
    stale-lease recovery fails it as `worker_interrupted`."""

    def processing_job(self, **fields):
        job = self.make_job(**fields)
        job.transition_to(S.PROCESSING)
        return job

    def assertFailedBeforeAiWork(self, job, client, code):
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, code))
        self.assertTrue(job.error_message)
        self.assertIsNotNone(job.completed_at)
        self.assertFalse(job.output_file)
        self.assertEqual(job.remote_job_id, '')  # the AI service never saw it
        self.assertIsNone(client.request)
        self.assertFalse((self.media_root / 'outputs').exists())

    def test_malformed_stored_settings_fail_immediately(self):
        without = lambda name: {k: v for k, v in TEXT_SETTINGS.items() if k != name}
        cases = {
            'missing fps': without('fps'),
            'missing width': without('width'),
            'missing frame_count': without('frame_count'),
            'width as text': {**TEXT_SETTINGS, 'width': '832'},
            'height as float': {**TEXT_SETTINGS, 'height': 480.0},
            'frame_count as bool': {**TEXT_SETTINGS, 'frame_count': True},
            'seed as text': {**TEXT_SETTINGS, 'seed': '7'},
            'enhance_prompt as text': {**TEXT_SETTINGS, 'enhance_prompt': 'yes'},
            'not an object': [TEXT_SETTINGS],
        }
        for name, stored in cases.items():
            with self.subTest(name):
                job = self.processing_job(settings=stored)
                client = FakeClient()
                with self.assertLogs('apps.generator.worker', level='WARNING') as logs:
                    process_job(job, client=client)
                self.assertFailedBeforeAiWork(job, client, 'invalid_job_settings')
                self.assertTrue(any('invalid_job_settings' in line and str(job.id) in line for line in logs.output))

    def test_settings_the_profile_no_longer_allows(self):
        cases = {
            'size': ({**TEXT_SETTINGS, 'width': 1024, 'height': 576}, GenerationJob.Mode.TEXT_TO_VIDEO),
            'frame count': ({**TEXT_SETTINGS, 'frame_count': 80}, GenerationJob.Mode.TEXT_TO_VIDEO),
            'fps': ({**TEXT_SETTINGS, 'fps': 24}, GenerationJob.Mode.TEXT_TO_VIDEO),
            'enhancement': ({**IMAGE_SETTINGS, 'enhance_prompt': True}, GenerationJob.Mode.IMAGE_TO_VIDEO),
        }
        for name, (stored, mode) in cases.items():
            with self.subTest(name):
                job = self.processing_job(mode=mode, settings=stored)
                if mode == GenerationJob.Mode.IMAGE_TO_VIDEO:
                    job.input_file.save('in.png', ContentFile(PNG), save=True)
                client = FakeClient()
                with self.assertLogs('apps.generator.worker', level='WARNING'):
                    process_job(job, client=client)
                self.assertFailedBeforeAiWork(job, client, 'unsupported_setting')

    def test_profile_of_the_other_mode_is_unsupported(self):
        job = self.processing_job(settings=IMAGE_SETTINGS)  # a text job naming the image profile
        client = FakeClient()
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            process_job(job, client=client)
        self.assertFailedBeforeAiWork(job, client, 'unsupported_setting')

    def test_image_job_without_its_image(self):
        no_reference = self.processing_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        missing_file = self.processing_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO, input_file='inputs/2026/10/gone.png')
        for job in (no_reference, missing_file):
            with self.subTest(input_file=job.input_file.name):
                client = FakeClient()
                with self.assertLogs('apps.generator.worker', level='WARNING'):
                    process_job(job, client=client)
                self.assertFailedBeforeAiWork(job, client, 'input_missing')

    def test_unexpected_preparation_error_fails_with_a_generic_message(self):
        job = self.processing_job()
        client = FakeClient()
        with mock.patch.object(worker, 'build_prompt', side_effect=RuntimeError('secret internal detail')), \
                self.assertLogs('apps.generator.worker', level='ERROR') as logs:
            process_job(job, client=client)
        self.assertFailedBeforeAiWork(job, client, 'internal_error')
        job.refresh_from_db()
        self.assertNotIn('secret', job.error_message)
        self.assertTrue(any('could not be prepared' in line for line in logs.output))

    def test_no_heartbeat_or_temporary_output_for_an_invalid_job(self):
        job = self.processing_job(settings={k: v for k, v in TEXT_SETTINGS.items() if k != 'fps'})
        with mock.patch.object(worker, 'JobHeartbeat') as heartbeat, \
                mock.patch.object(worker.tempfile, 'TemporaryDirectory') as tmp, \
                self.assertLogs('apps.generator.worker', level='WARNING'):
            process_job(job, client=FakeClient())
        heartbeat.assert_not_called()
        tmp.assert_not_called()

    def test_invalid_job_already_recovered_is_not_overwritten(self):
        job = self.processing_job(settings={k: v for k, v in TEXT_SETTINGS.items() if k != 'fps'})
        GenerationJob.objects.filter(pk=job.pk).update(status=S.FAILED, error_code='worker_interrupted')
        with self.assertLogs('apps.generator.worker', level='WARNING') as logs:
            process_job(job, client=FakeClient())
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'worker_interrupted'))
        self.assertTrue(any('lease lost' in line for line in logs.output))

    def test_valid_jobs_are_still_prepared_and_processed(self):
        text = self.processing_job(settings={**TEXT_SETTINGS, 'width': 480, 'height': 832, 'frame_count': 33,
                                             'seed': 0, 'enhance_prompt': True})
        image = self.processing_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        image.input_file.save('in.png', ContentFile(PNG), save=True)
        for job in (text, image):
            with self.subTest(mode=job.mode):
                client = FakeClient()
                process_job(job, client=client)
                job.refresh_from_db()
                self.assertEqual(job.status, S.COMPLETED)
                self.assertIsNotNone(client.request)
        self.assertEqual((client.request.width, client.request.height), (832, 480))


@unittest.skipUnless(HAS_FFMPEG, 'FFmpeg not available (PATH or imageio-ffmpeg)')
class FindFfmpegTests(unittest.TestCase):
    def test_path_first_then_bundled_binary_then_none(self):
        import sys
        from ai_service.client import mock as mock_module
        with mock.patch.object(mock_module.shutil, 'which', return_value='C:/tools/ffmpeg.exe'):
            self.assertEqual(find_ffmpeg(), 'C:/tools/ffmpeg.exe')
        bundled = SimpleNamespace(get_ffmpeg_exe=lambda: 'C:/venv/ffmpeg-bundled.exe')
        with mock.patch.object(mock_module.shutil, 'which', return_value=None),                 mock.patch.dict(sys.modules, {'imageio_ffmpeg': bundled}):
            self.assertEqual(find_ffmpeg(), 'C:/venv/ffmpeg-bundled.exe')

        def broken():
            raise RuntimeError('no binary for this platform')
        for module in (None, SimpleNamespace(get_ffmpeg_exe=broken)):  # not installed / no binary
            with self.subTest(module=module), mock.patch.object(mock_module.shutil, 'which', return_value=None),                     mock.patch.dict(sys.modules, {'imageio_ffmpeg': module}):
                self.assertIsNone(find_ffmpeg())

    def test_mock_client_without_ffmpeg_fails_with_its_code(self):
        from ai_service.client import mock as mock_module
        with mock.patch.object(mock_module, 'find_ffmpeg', return_value=None):
            client = MockClient(delay_seconds=0)
        submitted = []
        with self.assertRaises(AIServiceError) as ctx:
            client.generate(SimpleNamespace(job_id='j1'), Path('unused.mp4'), on_submitted=submitted.append)
        self.assertEqual(ctx.exception.code, 'mock_unavailable')
        self.assertEqual(submitted, ['mock-j1'])  # still tagged as mock output


class MockClientTests(TempMediaTestCase):
    def processing_job(self, **fields):
        job = self.make_job(**fields)
        job.transition_to(S.PROCESSING)
        return job

    def test_mock_worker_renders_verified_test_video(self):
        job = self.processing_job(settings={**TEXT_SETTINGS, 'frame_count': 17})
        process_job(job, client=MockClient(delay_seconds=0))
        job.refresh_from_db()
        self.assertEqual(job.status, S.COMPLETED)
        self.assertEqual(job.remote_job_id, f'mock-{job.id}')
        data = (self.media_root / job.output_file.name).read_bytes()
        self.assertGreater(len(data), 1024)
        self.assertEqual(data[4:8], b'ftyp')

    def test_completed_mock_job_is_flagged_in_api(self):
        job = self.processing_job(session_key=self.session_key(), settings={**TEXT_SETTINGS, 'frame_count': 17})
        process_job(job, client=MockClient(delay_seconds=0))
        self.assertIs(self.client.get(f'/api/v1/generations/{job.id}/').json()['result']['is_mock'], True)

    def test_mock_without_ffmpeg_fails_job(self):
        client = MockClient(delay_seconds=0)
        client.ffmpeg_path = None
        job = self.processing_job()
        process_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'mock_unavailable'))

    def test_mock_render_error_fails_job(self):
        job = self.processing_job()
        process_job(job, client=MockClient(delay_seconds=0, ffmpeg_path=str(self.media_root / 'no-ffmpeg.exe')))
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'mock_failed'))
        self.assertFalse(job.output_file)


class VerifyOutputTests(unittest.TestCase):
    def check(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.mp4'
            if content is not None:
                path.write_bytes(content)
            verify_mp4(path)

    def test_valid_mp4_passes(self):
        self.check(MP4)

    def test_missing_small_and_non_mp4_rejected(self):
        for label, content in {'missing': None, 'empty': b'', 'too small': MP4[:500], 'not mp4': b'x' * 4096}.items():
            with self.subTest(label):
                with self.assertRaises(AIServiceError) as ctx:
                    self.check(content)
                self.assertEqual(ctx.exception.code, 'invalid_output')


class ClientFactoryTests(unittest.TestCase):
    def test_mock_is_default(self):
        self.assertIsInstance(get_client(SimpleNamespace(AI_SERVICE_MODE='mock')), MockClient)

    def test_remote_requires_configuration(self):
        settings = SimpleNamespace(AI_SERVICE_MODE='remote', AI_SERVICE_BASE_URL='', AI_SERVICE_API_KEY='',
                                   AI_SERVICE_TIMEOUT=1, AI_SERVICE_POLL_INTERVAL=1, AI_SERVICE_MAX_WAIT=1,
                                   AI_SERVICE_MAX_VIDEO_BYTES=1)
        with self.assertRaises(AIServiceError) as ctx:
            get_client(settings)
        self.assertEqual(ctx.exception.code, 'service_not_configured')

    def test_remote_client_built_when_configured(self):
        settings = SimpleNamespace(AI_SERVICE_MODE='remote', AI_SERVICE_BASE_URL='https://example.invalid/',
                                   AI_SERVICE_API_KEY='test-key', AI_SERVICE_TIMEOUT=7,
                                   AI_SERVICE_POLL_INTERVAL=3, AI_SERVICE_MAX_WAIT=600,
                                   AI_SERVICE_MAX_VIDEO_BYTES=5_000_000)
        client = get_client(settings)
        self.assertIsInstance(client, RemoteClient)
        self.assertEqual(client.base_url, 'https://example.invalid')
        self.assertEqual((client.request_timeout, client.poll_interval, client.max_wait, client.max_video_bytes),
                         (7, 3, 600, 5_000_000))
