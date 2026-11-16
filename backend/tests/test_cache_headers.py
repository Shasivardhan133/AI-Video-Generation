"""C9: responses that belong to one session or user are never stored by caches.

Job details, history pages, videos (whole, ranged, 416) and CSRF tokens, and
their error responses, carry `Cache-Control: private, no-store` and
`Vary: Cookie`. Public, identical-for-everyone responses (health, profiles) are
left as they were.
"""

from django.contrib.auth import get_user_model
from django.test import Client

from .base import TempMediaTestCase

PRIVATE = 'private, no-store'


class PrivateCacheHeaderTests(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.session_key()
        self.job = self.make_completed_job(self.mine)
        self.detail = f'/api/v1/generations/{self.job.id}/'
        self.video = f'{self.detail}video/'

    def get(self, url, client=None, **headers):
        response = (client or self.client).get(url, headers=headers)
        if response.streaming:
            response.body = b''.join(response.streaming_content)  # lets the test client close the file
        return response

    def assertPrivate(self, response):
        self.assertEqual(response['Cache-Control'], PRIVATE)
        self.assertIn('Cookie', [value.strip() for value in response['Vary'].split(',')])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('X-Request-ID', response)

    def test_detail(self):
        response = self.get(self.detail)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertPrivate(response)

    def test_history_pages(self):
        for url in ('/api/v1/generations/', '/api/v1/generations/?page=1&page_size=5', '/api/v1/generations/?status=completed'):
            with self.subTest(url=url):
                response = self.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertPrivate(response)

    def test_empty_history_for_a_new_visitor(self):
        response = self.get('/api/v1/generations/', client=Client())
        self.assertEqual(response.json()['count'], 0)
        self.assertPrivate(response)

    def test_video_whole_ranged_download_and_unsatisfiable(self):
        cases = ((self.video, {}, 200), (self.video, {'Range': 'bytes=0-99'}, 206),
                 (f'{self.video}?download=1', {}, 200), (self.video, {'Range': 'bytes=999999-'}, 416))
        for url, headers, status in cases:
            with self.subTest(url=url, headers=headers):
                response = self.get(url, **headers)
                self.assertEqual(response.status_code, status)
                self.assertPrivate(response)
                if status != 416:
                    self.assertEqual(response['Content-Type'], 'video/mp4')
                    self.assertEqual(response['Accept-Ranges'], 'bytes')

    def test_errors_for_someone_elses_job_are_private_too(self):
        intruder = Client()
        intruder.get('/api/v1/csrf/')  # has a session of its own
        for url in (self.detail, self.video):
            with self.subTest(url=url):
                response = self.get(url, client=intruder)
                self.assertApiError(response, 404, 'not_found')
                self.assertPrivate(response)

    def test_unavailable_video_and_wrong_method_are_private(self):
        queued = self.make_job(self.mine)
        response = self.get(f'/api/v1/generations/{queued.id}/video/')
        self.assertApiError(response, 404, 'not_found')
        self.assertPrivate(response)
        response = self.client.delete(self.detail)
        self.assertApiError(response, 405, 'method_not_allowed')
        self.assertPrivate(response)

    def test_signed_in_user_responses_are_private(self):
        user = get_user_model().objects.create_user('alice', password='unused-test-password')
        client = Client()
        client.force_login(user)
        job = self.make_completed_job(user=user)
        for url in ('/api/v1/generations/', f'/api/v1/generations/{job.id}/', f'/api/v1/generations/{job.id}/video/'):
            with self.subTest(url=url):
                response = self.get(url, client=client)
                self.assertEqual(response.status_code, 200)
                self.assertPrivate(response)

    def test_csrf_token_and_created_job_are_private(self):
        self.assertPrivate(self.get('/api/v1/csrf/'))
        response = self.client.post('/api/v1/generations/text-to-video/', {'prompt': 'A calm lake'},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 202)
        self.assertPrivate(response)

    def test_public_responses_are_unchanged(self):
        for url in ('/api/v1/health/', '/api/v1/profiles/'):
            with self.subTest(url=url):
                self.assertNotIn('Cache-Control', self.get(url))

    def test_errors_raised_outside_the_views_are_private_too(self):
        # Middleware and the /api/ catch-all answer before any view runs.
        text_url = '/api/v1/generations/text-to-video/'
        cases = {
            'too large': lambda: self.client.post(text_url, b'{}', content_type='application/json',
                                                  CONTENT_LENGTH=str(64 * 1024 * 1024)),
            'bad Content-Length': lambda: self.client.post(text_url, b'{}', content_type='application/json',
                                                           CONTENT_LENGTH='12abc'),
            'missing slash': lambda: self.client.post(text_url.rstrip('/'), {}, content_type='application/json'),
            'malformed job id': lambda: self.get('/api/v1/generations/not-a-uuid/video/'),
            'unknown api path': lambda: self.get('/api/v1/nothing-here/'),
        }
        for name, request in cases.items():
            with self.subTest(name):
                response = request()
                self.assertIn(response.status_code, (400, 404, 413))
                self.assertIn('error', response.json())
                self.assertPrivate(response)

    def test_validation_and_csrf_errors_are_private(self):
        text_url = '/api/v1/generations/text-to-video/'
        invalid = self.client.post(text_url, {'prompt': ''}, content_type='application/json')
        self.assertApiError(invalid, 400, 'validation_error', fields=['prompt'])
        self.assertPrivate(invalid)
        browser = Client(enforce_csrf_checks=True)
        for url in (text_url, f'{self.detail}cancel/'):
            with self.subTest(url=url):
                refused = browser.post(url, {'prompt': 'a'}, content_type='application/json')
                self.assertApiError(refused, 403, 'forbidden')
                self.assertPrivate(refused)
