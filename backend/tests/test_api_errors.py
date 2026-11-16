"""G4: API errors are safe, structured JSON, with DEBUG on and off."""

import uuid
from unittest import mock

from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, override_settings

from ai_service.client import AIServiceError
from ai_service.client.remote import GENERATION_FAILED_MESSAGE, RemoteClient
from apps.generator.models import GenerationJob

from .base import PNG, TempMediaTestCase

TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'
SECRET_DETAIL = 'boom at D:\\FINAL YEAR PROJECT\\secret.py SECRET_KEY=abc123'
LEAK_MARKERS = ('boom', 'secret', 'SECRET_KEY', 'Traceback', 'D:\\', 'site-packages', 'File "')


def both_debug_modes(test):
    """Run the test body with DEBUG off and on."""
    def wrapper(self):
        for debug in (False, True):
            with self.subTest(DEBUG=debug), override_settings(DEBUG=debug):
                test(self)
    wrapper.__name__ = test.__name__
    return wrapper


class UnexpectedErrorTests(TempMediaTestCase):
    def assertSafe(self, response):
        text = response.content.decode()
        for marker in LEAK_MARKERS:
            self.assertNotIn(marker, text)

    @both_debug_modes
    def test_unexpected_error_on_create(self):
        with mock.patch.object(GenerationJob.objects, 'create', side_effect=RuntimeError(SECRET_DETAIL)), \
                self.assertLogs('apps.generator.exceptions', level='ERROR') as logs:
            response = self.client.post(TEXT_URL, {'prompt': 'A cat'}, content_type='application/json')
        error = self.assertApiError(response, 500, 'internal_error')
        self.assertEqual(error['message'], 'An unexpected error occurred. Please try again.')
        self.assertSafe(response)
        # The details are kept for the server log, with the request ID.
        self.assertIn(response['X-Request-ID'], logs.output[0])
        self.assertIn('boom', str(logs.records[0].exc_info[1]))

    @both_debug_modes
    def test_unexpected_error_on_read(self):
        with mock.patch('apps.generator.views.JobListView.get_queryset', side_effect=OSError(SECRET_DETAIL)), \
                self.assertLogs('apps.generator.exceptions', level='ERROR'):
            response = self.client.get('/api/v1/generations/')
        self.assertApiError(response, 500, 'internal_error')
        self.assertSafe(response)


class UnknownApiPathTests(TempMediaTestCase):
    @both_debug_modes
    def test_unknown_api_paths_return_json_404(self):
        for path in ('/api/v1/nope/', '/api/v1/generations/not-a-uuid/', f'/api/v1/generations/{uuid.uuid4()}/nope/',
                     '/api/v2/generations/', '/api/'):
            with self.subTest(path):
                response = self.client.get(path)
                self.assertEqual(response['Content-Type'], 'application/json')
                self.assertApiError(response, 404, 'not_found')
                self.assertNotIn('urlpatterns', response.content.decode())

    @both_debug_modes
    def test_post_to_unknown_api_path(self):
        response = self.client.post('/api/v1/nope/', {'prompt': 'a'}, content_type='application/json')
        self.assertApiError(response, 404, 'not_found')

    def test_trailing_slash_redirect_still_works(self):
        response = self.client.get('/api/v1/health')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], '/api/v1/health/')

    @both_debug_modes
    def test_non_api_pages_keep_html_404(self):
        response = self.client.get('/no-such-page/')
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response['Content-Type'].startswith('text/html'))


class MissingTrailingSlashTests(TempMediaTestCase):
    """C2: API writes without the trailing slash get a JSON 404, never an HTML 500 or 301."""

    def post_without_slash(self, client, url, **extra):
        if url == IMAGE_URL:
            data = {'image': SimpleUploadedFile('p.png', PNG, 'image/png'), 'prompt': 'A cat'}
            return client.post(url.rstrip('/'), data, **extra)
        return client.post(url.rstrip('/'), {'prompt': 'A cat'}, content_type='application/json', **extra)

    def assertMissingSlash(self, response):
        self.assertEqual(response['Content-Type'], 'application/json')
        error = self.assertApiError(response, 404, 'not_found')
        self.assertEqual(error['message'], 'Not found. API URLs must end with a slash.')
        self.assertNotIn('Location', response)

    @both_debug_modes
    def test_generation_posts_without_slash_get_json_404(self):
        for url in (TEXT_URL, IMAGE_URL):
            with self.subTest(url):
                self.assertMissingSlash(self.post_without_slash(self.client, url))
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertFalse((self.media_root / 'inputs').exists())
        self.assertEqual(Session.objects.count(), 0)

    @both_debug_modes
    def test_answered_before_csrf_so_nothing_runs(self):
        browser = Client(enforce_csrf_checks=True)
        for url in (TEXT_URL, IMAGE_URL):
            with self.subTest(url):
                self.assertMissingSlash(self.post_without_slash(browser, url))  # 404, not a CSRF 403
        self.assertEqual(GenerationJob.objects.count(), 0)

    @both_debug_modes
    def test_other_methods_and_paths_without_slash(self):
        for method in ('put', 'patch', 'delete', 'options'):
            with self.subTest(method):
                self.assertMissingSlash(getattr(self.client, method)(TEXT_URL.rstrip('/')))
        for path in ('/api', '/api/v1/nope', f'/api/v1/generations/{uuid.uuid4()}',
                     '/api/v1/generations/text-to-video?prompt=x'):
            with self.subTest(path):
                self.assertMissingSlash(self.client.post(path, {'prompt': 'a'}, content_type='application/json'))

    @both_debug_modes
    def test_get_and_head_still_redirect_to_the_slash_url(self):
        job = self.make_job()
        for path in ('/api/v1/health', '/api/v1/generations', f'/api/v1/generations/{job.id}'):
            for method in (self.client.get, self.client.head):
                with self.subTest(path=path, method=method.__name__):
                    response = method(path)
                    self.assertEqual(response.status_code, 301)
                    self.assertEqual(response['Location'], f'{path}/')

    @both_debug_modes
    def test_urls_with_slash_are_unchanged(self):
        self.assertEqual(self.client.post(TEXT_URL, {'prompt': 'A cat'}, content_type='application/json').status_code, 202)
        self.assertApiError(self.client.post(TEXT_URL, {}, content_type='application/json'), 400, 'validation_error',
                            fields=['prompt'])
        self.assertApiError(self.client.get(TEXT_URL), 405, 'method_not_allowed')
        self.assertApiError(self.client.post('/api/v1/nope/', {}, content_type='application/json'), 404, 'not_found')

    @both_debug_modes
    def test_non_api_paths_are_not_affected(self):
        response = self.client.post('/no-such-page', {'a': 'b'})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response['Content-Type'].startswith('text/html'))


class MissingTrailingSlashRateLimitTests(TempMediaTestCase):
    relax_generation_limits = False  # the real 5/minute limit

    def test_posts_without_slash_do_not_use_the_generation_allowance(self):
        browser = Client(enforce_csrf_checks=True)
        token = browser.get('/api/v1/csrf/').json()['csrf_token']
        for _ in range(5):
            for url in (TEXT_URL, IMAGE_URL):
                response = browser.post(url.rstrip('/'), {'prompt': 'A cat'}, content_type='application/json',
                                        headers={'X-CSRFToken': token})
                self.assertApiError(response, 404, 'not_found')
        for _ in range(5):
            response = browser.post(TEXT_URL, {'prompt': 'A cat'}, content_type='application/json',
                                    headers={'X-CSRFToken': token})
            self.assertEqual(response.status_code, 202)
        response = browser.post(TEXT_URL, {'prompt': 'A cat'}, content_type='application/json',
                                headers={'X-CSRFToken': token})
        self.assertApiError(response, 429, 'rate_limited')
        self.assertEqual(GenerationJob.objects.count(), 5)


class RequestParsingLimitTests(TempMediaTestCase):
    @both_debug_modes
    def test_too_many_form_fields(self):
        data = {f'field{i}': 'x' for i in range(1001)}
        data['image'] = SimpleUploadedFile('p.png', PNG, 'image/png')
        response = self.client.post(IMAGE_URL, data)
        error = self.assertApiError(response, 400, 'invalid_request')
        self.assertEqual(error['message'], 'The request has too many form fields or files.')
        self.assertEqual(GenerationJob.objects.count(), 0)

    @both_debug_modes
    def test_too_many_files(self):
        data = {f'file{i}': SimpleUploadedFile(f'{i}.png', PNG, 'image/png') for i in range(101)}
        response = self.client.post(IMAGE_URL, data)
        self.assertApiError(response, 400, 'invalid_request')
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertFalse((self.media_root / 'inputs').exists())

    @both_debug_modes
    def test_oversized_form_field(self):
        # Non-file fields above Django's 2.5 MB limit.
        response = self.client.post(IMAGE_URL, {'image': SimpleUploadedFile('p.png', PNG, 'image/png'),
                                                'prompt': 'x' * (3 * 1024 * 1024)})
        self.assertApiError(response, 413, 'request_too_large')
        self.assertEqual(GenerationJob.objects.count(), 0)

    @both_debug_modes
    def test_malformed_json_still_400(self):
        response = self.client.post(TEXT_URL, '{"prompt": ', content_type='application/json')
        self.assertApiError(response, 400, 'invalid_request')


class RemoteServiceErrorTests(SimpleTestCase):
    def run_failed_job(self, error):
        client = RemoteClient('https://ai.example.invalid', 'test-key', poll_interval=0, max_wait=5)
        replies = [{'job_id': 'remote-1'}, {'status': 'failed', 'error': error}]
        with mock.patch.object(RemoteClient, '_json', side_effect=lambda *a, **k: replies.pop(0)), \
                self.assertLogs('ai_service.client.remote', level='WARNING') as logs, \
                self.assertRaises(AIServiceError) as ctx:
            client.generate(mock.Mock(payload=lambda: {}, image_path=None), output_path=None)
        return ctx.exception, '\n'.join(logs.output)

    def test_internal_details_are_not_shown_to_users(self):
        exc, log = self.run_failed_job({'code': 'CUDA error at /content/wan.py',
                                        'message': 'Traceback (most recent call last): File "/content/drive/x.py"'})
        self.assertEqual((exc.code, exc.message), ('generation_failed', GENERATION_FAILED_MESSAGE))
        self.assertIn('/content/drive/x.py', log)  # kept for the server log only

    def test_safe_error_codes_are_kept(self):
        exc, _ = self.run_failed_job({'code': 'out_of_memory', 'message': 'GPU ran out of memory on /dev/nvidia0'})
        self.assertEqual((exc.code, exc.message), ('out_of_memory', GENERATION_FAILED_MESSAGE))

    def test_missing_or_malformed_error(self):
        for error in (None, 'just a string', {}):
            with self.subTest(error=error):
                exc, _ = self.run_failed_job(error)
                self.assertEqual((exc.code, exc.message), ('generation_failed', GENERATION_FAILED_MESSAGE))
