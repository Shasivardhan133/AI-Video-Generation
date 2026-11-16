"""Listing, details, and video delivery, including ownership protection."""

import uuid

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.generator.models import GenerationJob

from .base import MP4, TempMediaTestCase

LIST_URL = '/api/v1/generations/'
S = GenerationJob.Status
M = GenerationJob.Mode


def detail_url(job):
    return f'/api/v1/generations/{job.id}/'


def video_url(job):
    return f'/api/v1/generations/{job.id}/video/'


class OwnershipFixture(TempMediaTestCase):
    """`self.client` is the owner; `self.other` is a different browser session."""

    def setUp(self):
        super().setUp()
        self.mine = self.session_key()
        self.other = Client()
        self.theirs = self.session_key(self.other)
        self.assertNotEqual(self.mine, self.theirs)


class ListTests(OwnershipFixture):
    def ids(self, response):
        self.assertEqual(response.status_code, 200)
        return [item['job_id'] for item in response.json()['results']]

    def test_lists_only_own_jobs_newest_first(self):
        old = self.make_job(self.mine, minutes_ago=10)
        new = self.make_job(self.mine, minutes_ago=1)
        self.make_job(self.theirs)
        self.assertEqual(self.ids(self.client.get(LIST_URL)), [str(new.id), str(old.id)])

    def test_anonymous_session_cannot_see_user_owned_jobs(self):
        user = get_user_model().objects.create_user('alice', password='unused-test-password')
        self.make_job(self.mine, user=user)  # same session key, but owned by an account
        self.assertEqual(self.ids(self.client.get(LIST_URL)), [])

    def test_signed_in_user_sees_only_their_jobs(self):
        alice = get_user_model().objects.create_user('alice', password='unused-test-password')
        bob = get_user_model().objects.create_user('bob', password='unused-test-password')
        hers = self.make_job(user=alice)
        self.make_job(user=bob)
        self.make_job(self.mine)
        self.client.force_login(alice)
        self.assertEqual(self.ids(self.client.get(LIST_URL)), [str(hers.id)])

    def test_item_shape(self):
        job = self.make_completed_job(self.mine)
        item = self.client.get(LIST_URL).json()['results'][0]
        self.assertEqual(set(item), {'job_id', 'mode', 'prompt_preview', 'status', 'created_at', 'completed_at', 'video_available'})
        self.assertEqual(item['job_id'], str(job.id))
        self.assertEqual(item['status'], 'completed')
        self.assertIs(item['video_available'], True)

    def test_prompt_preview_truncated_to_80_chars(self):
        self.make_job(self.mine, prompt_text='y' * 80, minutes_ago=1)
        self.make_job(self.mine, prompt_text='x' * 81)
        previews = [item['prompt_preview'] for item in self.client.get(LIST_URL).json()['results']]
        self.assertEqual(previews, ['x' * 77 + '...', 'y' * 80])

    def test_pagination_defaults_and_limits(self):
        GenerationJob.objects.bulk_create([
            GenerationJob(session_key=self.mine, mode=M.TEXT_TO_VIDEO, prompt_text=str(i), settings={})
            for i in range(55)
        ])
        body = self.client.get(LIST_URL).json()
        self.assertEqual(set(body), {'count', 'next', 'previous', 'results'})
        self.assertEqual(body['count'], 55)
        self.assertEqual(len(body['results']), 20)
        self.assertIsNotNone(body['next'])
        self.assertIsNone(body['previous'])

        self.assertEqual(len(self.client.get(LIST_URL, {'page_size': 5}).json()['results']), 5)
        self.assertEqual(len(self.client.get(LIST_URL, {'page_size': 500}).json()['results']), 50)
        last = self.client.get(LIST_URL, {'page_size': 50, 'page': 2}).json()
        self.assertEqual(len(last['results']), 5)
        self.assertIsNone(last['next'])

    def test_pages_do_not_overlap(self):
        for i in range(7):
            self.make_job(self.mine, minutes_ago=i)
        first = self.ids(self.client.get(LIST_URL, {'page_size': 4}))
        second = self.ids(self.client.get(LIST_URL, {'page_size': 4, 'page': 2}))
        self.assertEqual(len(set(first + second)), 7)

    def test_page_out_of_range(self):
        self.make_job(self.mine)
        self.assertApiError(self.client.get(LIST_URL, {'page': 99}), 404, 'not_found')

    def test_filters(self):
        done = self.make_completed_job(self.mine)
        image = self.make_job(self.mine, mode=M.IMAGE_TO_VIDEO, status=S.FAILED)
        self.make_job(self.mine)
        self.assertEqual(self.ids(self.client.get(LIST_URL, {'status': 'completed'})), [str(done.id)])
        self.assertEqual(self.ids(self.client.get(LIST_URL, {'mode': 'image_to_video'})), [str(image.id)])
        self.assertEqual(self.ids(self.client.get(LIST_URL, {'status': 'failed', 'mode': 'text_to_video'})), [])

    def test_unknown_filter_values_are_ignored(self):
        self.make_job(self.mine)
        self.make_job(self.mine)
        self.assertEqual(len(self.ids(self.client.get(LIST_URL, {'status': 'bogus', 'mode': 'bogus'}))), 2)


class HistoryPagingTests(OwnershipFixture):
    """Paging must neither repeat nor skip a job, even when several jobs share one
    `created_at` (MySQL does not order equal values deterministically under LIMIT)."""

    def collect(self, page_size, **params):
        seen, page = [], 1
        while True:
            response = self.client.get(LIST_URL, {'page_size': page_size, 'page': page, **params})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            seen += [item['job_id'] for item in body['results']]
            if not body['next']:
                return seen
            page += 1

    def test_jobs_with_identical_timestamps_are_each_listed_once(self):
        moment = timezone.now()
        jobs = [self.make_job(self.mine, created_at=moment, status=S.FAILED if i % 2 else S.COMPLETED)
                for i in range(23)]
        GenerationJob.objects.filter(pk__in=[j.pk for j in jobs]).update(created_at=moment)
        expected = {str(job.id) for job in jobs}
        for page_size in (1, 4, 5, 7, 10):
            for params in ({}, {'status': 'failed'}, {'status': 'completed'}, {'mode': 'text_to_video'}):
                with self.subTest(page_size=page_size, **params):
                    seen = self.collect(page_size, **params)
                    self.assertEqual(len(seen), len(set(seen)), 'a job was listed twice')
                    wanted = {str(j.id) for j in jobs if params.get('status') in (None, j.status)}
                    self.assertEqual(set(seen), wanted, 'a job was skipped')
        self.assertEqual(set(self.collect(50)), expected)

    def test_invalid_page_and_page_size_values(self):
        for i in range(25):
            self.make_job(self.mine, minutes_ago=i)
        for page in ('abc', '0', '-1', '1.5', '99'):
            with self.subTest(page=page):
                self.assertApiError(self.client.get(LIST_URL, {'page': page}), 404, 'not_found')
        for page_size in ('abc', '0', '-1', ''):
            with self.subTest(page_size=page_size):
                body = self.client.get(LIST_URL, {'page_size': page_size}).json()
                self.assertEqual((body['count'], len(body['results'])), (25, 20))  # the default page size
        self.assertEqual(len(self.client.get(LIST_URL, {'page_size': '9999'}).json()['results']), 25)


class DetailTests(OwnershipFixture):
    def test_queued_job_detail(self):
        job = self.make_job(self.mine)
        response = self.client.get(detail_url(job))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {'job_id', 'mode', 'status', 'progress', 'prompt', 'settings', 'has_input_image',
                                     'created_at', 'started_at', 'completed_at', 'result', 'error'})
        self.assertEqual(body['job_id'], str(job.id))
        self.assertEqual(body['status'], 'queued')
        self.assertEqual(body['prompt'], job.prompt_text)
        self.assertEqual(body['settings'], job.settings)
        self.assertIsNone(body['progress'])
        self.assertIsNone(body['result'])
        self.assertIsNone(body['error'])
        self.assertIs(body['has_input_image'], False)

    def test_completed_mock_job_has_result_urls(self):
        job = self.make_completed_job(self.mine, remote_job_id=f'mock-{uuid.uuid4()}')
        body = self.client.get(detail_url(job)).json()
        self.assertEqual(body['progress'], 100)
        self.assertEqual(body['result'], {
            'preview_url': video_url(job),
            'download_url': f'{video_url(job)}?download=1',
            'download_filename': f'geetha-mock-{job.id}.mp4',
            'is_mock': True,
        })

    def test_non_mock_result_is_not_flagged_as_mock(self):
        job = self.make_completed_job(self.mine, remote_job_id='colab-123')
        self.assertIs(self.client.get(detail_url(job)).json()['result']['is_mock'], False)

    def test_failed_job_has_error(self):
        job = self.make_job(self.mine, status=S.FAILED, error_code='service_unavailable',
                            error_message='The AI service is unavailable.', request_id='req-1')
        body = self.client.get(detail_url(job)).json()
        self.assertIsNone(body['result'])
        self.assertEqual(body['error'], {'code': 'service_unavailable', 'message': 'The AI service is unavailable.',
                                         'request_id': 'req-1'})

    def test_has_input_image(self):
        job = self.make_job(self.mine, mode=M.IMAGE_TO_VIDEO, input_file='inputs/2026/10/a.png')
        self.assertIs(self.client.get(detail_url(job)).json()['has_input_image'], True)

    def test_detail_never_exposes_file_paths(self):
        job = self.make_completed_job(self.mine, mode=M.IMAGE_TO_VIDEO, input_file='inputs/2026/10/a.png')
        text = self.client.get(detail_url(job)).content.decode()
        self.assertNotIn('outputs/', text)
        self.assertNotIn('inputs/', text)
        self.assertNotIn(str(self.media_root), text)

    def test_other_sessions_job_looks_missing(self):
        job = self.make_job(self.theirs)
        self.assertApiError(self.client.get(detail_url(job)), 404, 'not_found')
        self.assertEqual(self.other.get(detail_url(job)).status_code, 200)

    def test_unknown_job_is_not_found(self):
        response = self.client.get(f'/api/v1/generations/{uuid.uuid4()}/')
        self.assertApiError(response, 404, 'not_found')

    def test_detail_is_read_only(self):
        job = self.make_job(self.mine)
        self.assertApiError(self.client.delete(detail_url(job)), 405, 'method_not_allowed')
        self.assertTrue(GenerationJob.objects.filter(pk=job.pk).exists())


class VideoTests(OwnershipFixture):
    def get(self, url, client=None, **params):
        response = (client or self.client).get(url, params)
        if response.streaming:
            # Reading to the end lets the test client close the file. Calling
            # response.close() directly would also close the test DB connection.
            response.body = b''.join(response.streaming_content)
        return response

    def test_streams_owned_video_inline(self):
        job = self.make_completed_job(self.mine)
        response = self.get(video_url(job))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'video/mp4')
        self.assertTrue(response['Content-Disposition'].startswith('inline'))
        self.assertEqual(response.body, MP4)

    def test_download_is_an_attachment_with_safe_name(self):
        job = self.make_completed_job(self.mine)
        response = self.get(video_url(job), download='1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Disposition'], f'attachment; filename="geetha-mock-{job.id}.mp4"')

    def test_other_sessions_video_looks_missing(self):
        job = self.make_completed_job(self.theirs)
        self.assertApiError(self.get(video_url(job)), 404, 'not_found')
        self.assertEqual(self.get(video_url(job), client=self.other).status_code, 200)

    def test_signed_in_user_cannot_fetch_anonymous_video(self):
        job = self.make_completed_job(self.mine)
        user = get_user_model().objects.create_user('alice', password='unused-test-password')
        intruder = Client()
        intruder.force_login(user)
        self.assertApiError(self.get(video_url(job), client=intruder), 404, 'not_found')

    def test_unfinished_jobs_have_no_video(self):
        for status in (S.QUEUED, S.PROCESSING, S.FAILED):
            with self.subTest(status=status):
                job = self.make_job(self.mine, status=status)
                self.assertApiError(self.get(video_url(job)), 404, 'not_found')

    def test_missing_output_file_is_not_found(self):
        job = self.make_completed_job(self.mine)
        (self.media_root / job.output_file.name).unlink()
        self.assertApiError(self.get(video_url(job)), 404, 'not_found')

    def test_unknown_job_video_is_not_found(self):
        self.assertApiError(self.get(f'/api/v1/generations/{uuid.uuid4()}/video/'), 404, 'not_found')

    def test_media_folder_is_not_served_directly(self):
        job = self.make_completed_job(self.mine)
        self.assertEqual(self.get(f'/media/{job.output_file.name}').status_code, 404)
