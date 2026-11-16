"""CSRF protection on generation writes, with Django's CSRF checks really enforced.

The default test client skips CSRF checks, so every client here is created with
enforce_csrf_checks=True. CSRF rejections are 403 `forbidden` responses whose
message starts with "CSRF Failed"; ordinary validation errors are 400.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.generator.models import GenerationJob

from .base import PNG, TempMediaTestCase

TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'
CSRF_URL = '/api/v1/csrf/'


def png():
    return SimpleUploadedFile('photo.png', PNG, content_type='image/png')


class CsrfTestCase(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.browser = Client(enforce_csrf_checks=True)

    def fetch_token(self, client=None):
        """What the frontend does: GET /api/v1/csrf/, keep the cookie, use the token."""
        response = (client or self.browser).get(CSRF_URL)
        self.assertEqual(response.status_code, 200)
        return response.json()['csrf_token']

    def post_text(self, data, token=None, client=None, **headers):
        if token is not None:
            headers['X-CSRFToken'] = token
        return (client or self.browser).post(TEXT_URL, data, content_type='application/json', headers=headers)

    def post_image(self, data, token=None, client=None, **headers):
        if token is not None:
            headers['X-CSRFToken'] = token
        return (client or self.browser).post(IMAGE_URL, data, headers=headers)

    def assertCsrfRejected(self, response):
        error = self.assertApiError(response, 403, 'forbidden')
        self.assertTrue(error['message'].startswith('CSRF Failed'), error['message'])
        self.assertEqual(GenerationJob.objects.count(), 0)


class CsrfTokenEndpointTests(CsrfTestCase):
    def test_returns_token_and_sets_cookie(self):
        response = self.browser.get(CSRF_URL)
        self.assertEqual(response.status_code, 200)
        token = response.json()['csrf_token']
        self.assertEqual(len(token), 64)
        self.assertIn(settings.CSRF_COOKIE_NAME, response.cookies)

    def test_does_not_create_a_session(self):
        self.browser.get(CSRF_URL)
        self.assertEqual(Session.objects.count(), 0)


class TextToVideoCsrfTests(CsrfTestCase):
    def test_no_token_rejected_before_validation(self):
        # Valid and invalid bodies both get 403, so CSRF runs before validation.
        self.assertCsrfRejected(self.post_text({'prompt': 'A lighthouse at dusk'}))
        self.assertCsrfRejected(self.post_text({'prompt': ''}))

    def test_valid_token_reaches_normal_validation(self):
        token = self.fetch_token()
        self.assertApiError(self.post_text({'prompt': ''}, token), 400, 'validation_error', fields=['prompt'])
        self.assertApiError(self.post_text({'prompt': 'a', 'settings': {'fps': 30}}, token),
                            400, 'unsupported_setting', fields=['settings'])

    def test_valid_token_creates_job_from_prompt_alone(self):
        token = self.fetch_token()
        response = self.post_text({'prompt': 'A lighthouse at dusk'}, token)
        self.assertEqual(response.status_code, 202)
        job = GenerationJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual(job.mode, GenerationJob.Mode.TEXT_TO_VIDEO)
        self.assertFalse(job.input_file)

    def test_cookie_without_header_rejected(self):
        self.fetch_token()
        self.assertCsrfRejected(self.post_text({'prompt': 'a'}))

    def test_header_without_cookie_rejected(self):
        token = self.fetch_token(Client(enforce_csrf_checks=True))  # cookie stays in the other client
        self.assertCsrfRejected(self.post_text({'prompt': 'a'}, token))

    def test_invalid_tokens_rejected(self):
        self.fetch_token()
        for bad in ['', 'abc', 'x' * 64, '!' * 32]:
            with self.subTest(token=bad):
                self.assertCsrfRejected(self.post_text({'prompt': 'a'}, bad))

    def test_token_from_another_browser_rejected(self):
        self.fetch_token()
        other_token = self.fetch_token(Client(enforce_csrf_checks=True))
        self.assertCsrfRejected(self.post_text({'prompt': 'a'}, other_token))

    def test_cross_origin_request_rejected_even_with_valid_token(self):
        token = self.fetch_token()
        self.assertCsrfRejected(self.post_text({'prompt': 'a'}, token, Origin='https://evil.example'))

    def test_same_origin_header_accepted(self):
        token = self.fetch_token()
        self.assertEqual(self.post_text({'prompt': 'a'}, token, Origin='http://testserver').status_code, 202)

    def test_signed_in_users_also_need_a_token(self):
        user = get_user_model().objects.create_user('alice', password='unused-test-password')
        self.browser.force_login(user)
        self.assertCsrfRejected(self.post_text({'prompt': 'a'}))
        self.assertEqual(self.post_text({'prompt': 'a'}, self.fetch_token()).status_code, 202)


class ImageToVideoCsrfTests(CsrfTestCase):
    def test_no_token_rejected_before_validation(self):
        self.assertCsrfRejected(self.post_image({'image': png(), 'prompt': 'Make it wave'}))
        self.assertCsrfRejected(self.post_image({'image': SimpleUploadedFile('e.png', b'')}))
        self.assertFalse((self.media_root / 'inputs').exists())

    def test_valid_token_reaches_image_validation(self):
        token = self.fetch_token()
        self.assertApiError(self.post_image({'image': SimpleUploadedFile('e.png', b'')}, token),
                            400, 'invalid_image', fields=['image'])
        self.assertApiError(self.post_image({'prompt': 'a'}, token), 400, 'validation_error', fields=['image'])

    def test_valid_token_creates_job_with_and_without_prompt(self):
        token = self.fetch_token()
        for data in ({'image': png(), 'prompt': 'Make it wave'}, {'image': png()}):
            with self.subTest(prompt=data.get('prompt')):
                response = self.post_image(data, token)
                self.assertEqual(response.status_code, 202)
                job = GenerationJob.objects.get(pk=response.json()['job_id'])
                self.assertEqual(job.mode, GenerationJob.Mode.IMAGE_TO_VIDEO)
                self.assertEqual(job.prompt_text, data.get('prompt', ''))

    def test_form_field_token_accepted(self):
        # Django also accepts the token as a multipart form field.
        token = self.fetch_token()
        response = self.post_image({'image': png(), 'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 202)

    def test_missing_invalid_and_mismatched_tokens_rejected(self):
        self.assertCsrfRejected(self.post_image({'image': png()}, 'abc'))  # no cookie yet
        self.fetch_token()
        self.assertCsrfRejected(self.post_image({'image': png()}))
        self.assertCsrfRejected(self.post_image({'image': png()}, 'abc'))
        self.assertCsrfRejected(self.post_image({'image': png()}, self.fetch_token(Client(enforce_csrf_checks=True))))


class ReadsWithoutTokenTests(CsrfTestCase):
    """GET endpoints keep working for a browser that never fetched a token."""

    def test_get_endpoints(self):
        self.assertEqual(self.browser.get('/api/v1/health/').status_code, 200)
        self.assertEqual(self.browser.get('/api/v1/profiles/').status_code, 200)
        self.assertEqual(self.browser.get('/api/v1/generations/').status_code, 200)

    def test_owned_job_detail_and_video(self):
        job = self.make_completed_job(self.session_key(self.browser))
        self.assertEqual(self.browser.get(f'/api/v1/generations/{job.id}/').status_code, 200)
        response = self.browser.get(f'/api/v1/generations/{job.id}/video/')
        self.assertEqual(response.status_code, 200)
        b''.join(response.streaming_content)  # let the test client close the file

    def test_ownership_still_enforced(self):
        job = self.make_job(self.session_key(Client()))
        self.assertApiError(self.browser.get(f'/api/v1/generations/{job.id}/'), 404, 'not_found')
