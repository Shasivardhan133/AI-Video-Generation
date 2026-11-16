"""Session rows: reads must not create them; guest job creation must.

Every client enforces CSRF, like a real browser. Session rows are counted in the
test database before and after each request.
"""

import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.generator.models import GenerationJob

from .base import MP4, PNG, TempMediaTestCase

TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'
LIST_URL = '/api/v1/generations/'


def png():
    return SimpleUploadedFile('photo.png', PNG, content_type='image/png')


class SessionTestCase(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.browser = Client(enforce_csrf_checks=True)

    def get(self, url, client=None, **params):
        response = (client or self.browser).get(url, params)
        if response.streaming:
            b''.join(response.streaming_content)  # let the test client close the file
        return response

    def create_text_job(self, client=None, prompt='A lighthouse at dusk'):
        client = client or self.browser
        token = client.get('/api/v1/csrf/').json()['csrf_token']
        response = client.post(TEXT_URL, {'prompt': prompt}, content_type='application/json',
                               headers={'X-CSRFToken': token})
        self.assertEqual(response.status_code, 202, response.content)
        return GenerationJob.objects.get(pk=response.json()['job_id'])

    def assertNoSessionCreated(self, response, before=0):
        self.assertEqual(Session.objects.count(), before)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)


class AnonymousReadsCreateNoSessionTests(SessionTestCase):
    """A first-time visitor with no session cookie."""

    def test_health_profiles_and_csrf_token(self):
        for url in ('/api/v1/health/', '/api/v1/profiles/', '/api/v1/csrf/'):
            with self.subTest(url):
                self.assertNoSessionCreated(self.get(url))

    def test_generation_list_is_empty_without_a_session(self):
        self.make_job(self.session_key(Client()))  # someone else's job
        before = Session.objects.count()
        response = self.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'count': 0, 'next': None, 'previous': None, 'results': []})
        self.assertNoSessionCreated(response, before)

    def test_list_with_filters_and_pages(self):
        for params in ({'status': 'completed'}, {'mode': 'text_to_video'}, {'page_size': 30}):
            with self.subTest(params):
                self.assertNoSessionCreated(self.get(LIST_URL, **params))

    def test_detail_and_video_of_other_or_unknown_jobs(self):
        theirs = self.make_completed_job(self.session_key(Client()))
        before = Session.objects.count()
        for url in (f'/api/v1/generations/{theirs.id}/', f'/api/v1/generations/{theirs.id}/video/',
                    f'/api/v1/generations/{uuid.uuid4()}/', f'/api/v1/generations/{uuid.uuid4()}/video/'):
            with self.subTest(url):
                response = self.get(url)
                self.assertApiError(response, 404, 'not_found')
                self.assertNoSessionCreated(response, before)

    def test_homepage_load_flow(self):
        # What the frontend does on page load before any generation.
        for url in ('/', '/api/v1/generations/?page_size=30', '/api/v1/csrf/'):
            self.assertNoSessionCreated(self.get(url))

    def test_stale_session_cookie(self):
        # A cookie whose session row no longer exists (expired or cleared).
        self.browser.cookies[settings.SESSION_COOKIE_NAME] = 'a' * 32
        response = self.get(LIST_URL)
        self.assertEqual(response.json()['count'], 0)
        self.assertEqual(Session.objects.count(), 0)


class RejectedWritesCreateNoSessionTests(SessionTestCase):
    def test_csrf_rejected_writes(self):
        self.assertEqual(self.browser.post(TEXT_URL, {'prompt': 'a'}, content_type='application/json').status_code, 403)
        self.assertEqual(self.browser.post(IMAGE_URL, {'image': png()}).status_code, 403)
        self.assertEqual(Session.objects.count(), 0)
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_invalid_writes_with_valid_token(self):
        token = self.browser.get('/api/v1/csrf/').json()['csrf_token']
        headers = {'X-CSRFToken': token}
        self.assertEqual(self.browser.post(TEXT_URL, {'prompt': ''}, content_type='application/json',
                                           headers=headers).status_code, 400)
        self.assertEqual(self.browser.post(IMAGE_URL, {'image': SimpleUploadedFile('e.png', b'')},
                                           headers=headers).status_code, 400)
        self.assertEqual(Session.objects.count(), 0)


class GuestCreationAndOwnershipTests(SessionTestCase):
    def test_first_text_job_creates_one_session_and_sets_cookie(self):
        job = self.create_text_job()
        self.assertEqual(Session.objects.count(), 1)
        session_key = self.browser.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(job.session_key, session_key)
        self.assertTrue(Session.objects.filter(session_key=session_key).exists())

    def test_first_image_job_creates_one_session(self):
        token = self.browser.get('/api/v1/csrf/').json()['csrf_token']
        response = self.browser.post(IMAGE_URL, {'image': png()}, headers={'X-CSRFToken': token})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(Session.objects.count(), 1)
        job = GenerationJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual(job.session_key, self.browser.cookies[settings.SESSION_COOKIE_NAME].value)

    def test_later_jobs_reuse_the_same_session(self):
        first = self.create_text_job(prompt='one')
        second = self.create_text_job(prompt='two')
        self.assertEqual(Session.objects.count(), 1)
        self.assertEqual(first.session_key, second.session_key)

    def test_guest_reads_own_history_without_new_sessions(self):
        job = self.create_text_job()
        job.transition_to(GenerationJob.Status.PROCESSING)
        job.output_file.save(f'{job.id}.mp4', ContentFile(MP4), save=False)
        job.transition_to(GenerationJob.Status.COMPLETED, output_file=job.output_file)

        self.assertEqual([item['job_id'] for item in self.get(LIST_URL).json()['results']], [str(job.id)])
        self.assertEqual(self.get(f'/api/v1/generations/{job.id}/').status_code, 200)
        self.assertEqual(self.get(f'/api/v1/generations/{job.id}/video/').status_code, 200)
        self.assertEqual(Session.objects.count(), 1)

    def test_other_visitors_still_cannot_see_guest_jobs(self):
        job = self.create_text_job()
        stranger = Client(enforce_csrf_checks=True)
        self.assertEqual(self.get(LIST_URL, client=stranger).json()['count'], 0)
        self.assertApiError(self.get(f'/api/v1/generations/{job.id}/', client=stranger), 404, 'not_found')
        self.assertApiError(self.get(f'/api/v1/generations/{job.id}/video/', client=stranger), 404, 'not_found')
        self.assertEqual(Session.objects.count(), 1)

    def test_stale_cookie_then_create_starts_a_fresh_session(self):
        self.browser.cookies[settings.SESSION_COOKIE_NAME] = 'a' * 32
        job = self.create_text_job()
        self.assertEqual(Session.objects.count(), 1)
        self.assertNotEqual(job.session_key, 'a' * 32)
        self.assertEqual(self.get(LIST_URL).json()['count'], 1)


class ExistingGuestSessionTests(SessionTestCase):
    """A returning guest whose session already exists (like the real data)."""

    def setUp(self):
        super().setUp()
        self.key = self.session_key(self.browser)
        self.jobs = [self.make_completed_job(self.key, minutes_ago=5), self.make_job(self.key)]
        self.row = Session.objects.values().get(session_key=self.key)

    def test_history_is_returned(self):
        ids = [item['job_id'] for item in self.get(LIST_URL).json()['results']]
        self.assertEqual(ids, [str(self.jobs[1].id), str(self.jobs[0].id)])

    def test_reads_leave_the_session_row_unchanged(self):
        self.get(LIST_URL)
        self.get(f'/api/v1/generations/{self.jobs[0].id}/')
        self.get(f'/api/v1/generations/{self.jobs[0].id}/video/')
        self.assertEqual(Session.objects.count(), 1)
        self.assertEqual(Session.objects.values().get(session_key=self.key), self.row)

    def test_new_job_joins_existing_history(self):
        job = self.create_text_job()
        self.assertEqual(job.session_key, self.key)
        self.assertEqual(Session.objects.count(), 1)
        self.assertEqual(self.get(LIST_URL).json()['count'], 3)


class SignedInUserTests(SessionTestCase):
    def test_reads_add_no_sessions_beyond_login(self):
        user = get_user_model().objects.create_user('alice', password='unused-test-password')
        mine = self.make_job(user=user)
        self.browser.force_login(user)
        before = Session.objects.count()
        self.assertEqual([item['job_id'] for item in self.get(LIST_URL).json()['results']], [str(mine.id)])
        self.assertEqual(self.get(f'/api/v1/generations/{mine.id}/').status_code, 200)
        self.assertEqual(Session.objects.count(), before)
