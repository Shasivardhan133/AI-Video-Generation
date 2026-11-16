"""C8: one predictable, safe name for a generated video, in the API and its headers.

The detail response's `result.download_filename` (used by the frontend's
`download` attribute) and the video endpoint's Content-Disposition name are
the same: 'geetha-<job id>.mp4', or 'geetha-mock-<job id>.mp4' for mock output.
"""

import re
import uuid

from django.test import SimpleTestCase

from apps.generator.filenames import is_mock_video, video_filename
from apps.generator.models import GenerationJob

from .base import MP4, TempMediaTestCase

SAFE_NAME = re.compile(r'^geetha-(mock-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4$')


class VideoFilenameTests(SimpleTestCase):
    def job(self, remote_job_id):
        return GenerationJob(id=uuid.UUID('12345678-1234-4234-8234-123456789abc'), remote_job_id=remote_job_id)

    def test_mock_and_real_names(self):
        self.assertEqual(video_filename(self.job('mock-abc')), 'geetha-mock-12345678-1234-4234-8234-123456789abc.mp4')
        self.assertEqual(video_filename(self.job('colab-123')), 'geetha-12345678-1234-4234-8234-123456789abc.mp4')
        self.assertIs(is_mock_video(self.job('mock-abc')), True)
        self.assertIs(is_mock_video(self.job('colab-mock-1')), False)  # only the prefix counts

    def test_name_never_depends_on_service_supplied_text(self):
        # The remote job ID comes from the AI service; it only selects the prefix.
        for remote in ('', 'colab-1', '../../etc/passwd', 'a"b\r\nX: y', 'mock-"; evil=1', 'mock-‮'):
            with self.subTest(remote=remote):
                self.assertRegex(video_filename(self.job(remote)), SAFE_NAME)


class DownloadFilenameApiTests(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.session_key()

    def get_video(self, job, **params):
        response = self.client.get(f'/api/v1/generations/{job.id}/video/', params)
        response.body = b''.join(response.streaming_content)  # lets the test client close the file
        return response

    def assertSameName(self, job, expected):
        result = self.client.get(f'/api/v1/generations/{job.id}/').json()['result']
        self.assertEqual(result['download_filename'], expected)
        self.assertEqual(result['download_url'], f'/api/v1/generations/{job.id}/video/?download=1')

        download = self.get_video(job, download='1')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download['Content-Disposition'], f'attachment; filename="{expected}"')
        self.assertEqual(download['Content-Type'], 'video/mp4')
        self.assertEqual(download.body, MP4)

        inline = self.get_video(job)
        self.assertEqual(inline.status_code, 200)
        self.assertEqual(inline['Content-Disposition'], f'inline; filename="{expected}"')
        self.assertEqual(inline['Content-Type'], 'video/mp4')
        self.assertEqual(inline.body, MP4)

    def test_mock_video_names_match(self):
        job = self.make_completed_job(self.mine, remote_job_id=f'mock-{uuid.uuid4()}')
        self.assertSameName(job, f'geetha-mock-{job.id}.mp4')

    def test_real_video_names_match(self):
        job = self.make_completed_job(self.mine, remote_job_id='colab-123')
        self.assertSameName(job, f'geetha-{job.id}.mp4')

    def test_ranged_download_and_playback_use_the_same_name(self):
        job = self.make_completed_job(self.mine)
        for params, disposition in (({'download': '1'}, 'attachment'), ({}, 'inline'), ({'download': 'yes'}, 'inline')):
            with self.subTest(params=params):
                response = self.client.get(f'/api/v1/generations/{job.id}/video/', params,
                                           headers={'Range': 'bytes=0-9'})
                response.body = b''.join(response.streaming_content)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response['Content-Disposition'],
                                 f'{disposition}; filename="geetha-mock-{job.id}.mp4"')
                self.assertEqual(response.body, MP4[:10])

    def test_prompt_never_appears_in_the_name(self):
        job = self.make_completed_job(self.mine, prompt_text='"; filename=evil.exe ../../secret ‮')
        self.assertRegex(self.client.get(f'/api/v1/generations/{job.id}/').json()['result']['download_filename'],
                         SAFE_NAME)
        self.assertEqual(self.get_video(job, download='1')['Content-Disposition'],
                         f'attachment; filename="geetha-mock-{job.id}.mp4"')

    def test_unfinished_job_has_no_download_name(self):
        job = self.make_job(self.mine)
        self.assertIsNone(self.client.get(f'/api/v1/generations/{job.id}/').json()['result'])
