"""G1 and G5: the early request-size guard and the upload settings.

Units: 1 MB = 1,048,576 bytes (MiB). Images may be up to 25 MB; a whole
generation request may be up to 26 MB (declared Content-Length).
"""

import json

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase

from apps.generator.middleware import GenerationRequestSizeMiddleware
from apps.generator.models import GenerationJob
from apps.generator.validators import MAX_IMAGE_BYTES

from .base import PNG, TempMediaTestCase, make_png
from .test_throttling import ThrottleTestCase

MB = 1024 * 1024
LIMIT = 26 * MB
TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'


class SizeSettingsTests(SimpleTestCase):
    def test_limits_and_units(self):
        self.assertEqual(settings.GENERATION_MAX_REQUEST_BYTES, 26 * 1024 * 1024)
        self.assertEqual(MAX_IMAGE_BYTES, 25 * 1024 * 1024)
        # A 25 MB image leaves 1 MB for the multipart framing and other fields.
        self.assertEqual(settings.GENERATION_MAX_REQUEST_BYTES - MAX_IMAGE_BYTES, MB)

    def test_form_field_limit_is_djangos_default(self):
        # The misleading 11 MB override is gone; 2.5 MB applies to non-file fields only.
        self.assertEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 2621440)

    def test_guard_runs_before_anything_reads_the_body(self):
        order = settings.MIDDLEWARE
        guard = order.index('apps.generator.middleware.GenerationRequestSizeMiddleware')
        self.assertGreater(guard, order.index('apps.generator.middleware.RequestIDMiddleware'))
        self.assertLess(guard, order.index('django.contrib.sessions.middleware.SessionMiddleware'))
        self.assertLess(guard, order.index('django.middleware.csrf.CsrfViewMiddleware'))


class GuardUnitTests(SimpleTestCase):
    """The middleware on its own: exact boundaries and the Content-Length policy."""

    def run_guard(self, content_length, method='POST', path=TEXT_URL):
        passed = []
        guard = GenerationRequestSizeMiddleware(lambda request: passed.append(request) or HttpResponse('passed'))
        request = RequestFactory().generic(method, path, b'')
        request.request_id = 'req-test'
        if content_length is None:
            request.META.pop('CONTENT_LENGTH', None)
        else:
            request.META['CONTENT_LENGTH'] = content_length
        return guard(request), bool(passed)

    def assertRejected(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response['Content-Type'], 'application/json')
        error = json.loads(response.content)['error']
        self.assertEqual(set(error), {'code', 'message', 'request_id'})
        self.assertEqual((error['code'], error['request_id']), (code, 'req-test'))
        self.assertTrue(error['message'])

    def test_boundary(self):
        for path in (TEXT_URL, IMAGE_URL):
            with self.subTest(path):
                self.assertTrue(self.run_guard(str(LIMIT - 1), path=path)[1])
                self.assertTrue(self.run_guard(str(LIMIT), path=path)[1])
                response, passed = self.run_guard(str(LIMIT + 1), path=path)
                self.assertFalse(passed)
                self.assertRejected(response, 413, 'request_too_large')
                self.assertEqual(json.loads(response.content)['error']['message'],
                                 'The request is too large. Images must be 25 MB or smaller.')

    def test_huge_declared_lengths(self):
        for value in ('99999999999', '9' * 20):
            response, passed = self.run_guard(value)
            self.assertFalse(passed)
            self.assertRejected(response, 413, 'request_too_large')

    def test_missing_or_empty_content_length_passes_through(self):
        # Documented policy: Django reads such a body as empty, so normal
        # validation (or CSRF) rejects the request without reading any data.
        self.assertTrue(self.run_guard(None)[1])
        self.assertTrue(self.run_guard('')[1])

    def test_malformed_content_length_rejected(self):
        for value in ('abc', '-1', '1.5', ' 10', '10 ', '0x10', '１２', '9' * 21):
            with self.subTest(value=value):
                response, passed = self.run_guard(value)
                self.assertFalse(passed)
                self.assertRejected(response, 400, 'invalid_request')

    def test_other_requests_untouched(self):
        self.assertTrue(self.run_guard(str(LIMIT * 10), method='GET', path='/api/v1/generations/')[1])
        self.assertTrue(self.run_guard(str(LIMIT * 10), method='GET', path=TEXT_URL)[1])
        self.assertTrue(self.run_guard(str(LIMIT * 10), path='/api/v1/health/')[1])
        self.assertTrue(self.run_guard('abc', path='/admin/login/')[1])


class OversizedRequestTests(TempMediaTestCase):
    def test_oversized_text_request(self):
        response = self.client.post(TEXT_URL, {'prompt': 'A cat'}, content_type='application/json',
                                    CONTENT_LENGTH=str(LIMIT + 1))
        self.assertApiError(response, 413, 'request_too_large')
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertEqual(Session.objects.count(), 0)

    def test_oversized_image_request_stores_nothing(self):
        response = self.client.post(IMAGE_URL, {'image': SimpleUploadedFile('a.png', PNG, 'image/png')},
                                    CONTENT_LENGTH=str(LIMIT + 1))
        self.assertApiError(response, 413, 'request_too_large')
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertFalse((self.media_root / 'inputs').exists())

    def test_rejected_before_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        response = browser.post(TEXT_URL, {'prompt': 'a'}, content_type='application/json', CONTENT_LENGTH=str(LIMIT + 1))
        self.assertApiError(response, 413, 'request_too_large')

    def test_real_multipart_bodies_at_and_over_the_limit(self):
        boundary = 'GeethaTestBoundary'
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="big.png"\r\n'
                'Content-Type: image/png\r\n\r\n').encode()
        tail = f'\r\n--{boundary}--\r\n'.encode()

        def post(total):
            body = head + make_png(total_size=total - len(head) - len(tail)) + tail
            self.assertEqual(len(body), total)
            return self.client.generic('POST', IMAGE_URL, body, content_type=f'multipart/form-data; boundary={boundary}')

        # Exactly 26 MB passes the guard and reaches validation (the image inside is just over 25 MB).
        self.assertApiError(post(LIMIT), 400, 'image_too_large', fields=['image'])
        # One byte more is stopped by the guard before parsing.
        error = self.assertApiError(post(LIMIT + 1), 413, 'request_too_large')
        self.assertEqual(error['message'], 'The request is too large. Images must be 25 MB or smaller.')
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertFalse((self.media_root / 'inputs').exists())

    def test_json_bodies_are_capped_by_the_form_data_limit(self):
        # DRF 3.18 reads JSON through request.body, so DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB) applies.
        def body(total):
            start, end = b'{"prompt": "A lighthouse at dusk"', b'}'
            return start + b' ' * (total - len(start) - len(end)) + end

        limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        response = self.client.generic('POST', TEXT_URL, body(limit), content_type='application/json')
        self.assertEqual(response.status_code, 202, response.content[:300])
        error = self.assertApiError(self.client.generic('POST', TEXT_URL, body(limit + 1), content_type='application/json'),
                                    413, 'request_too_large')
        self.assertEqual(error['message'], 'The request is too large.')
        self.assertEqual(GenerationJob.objects.count(), 1)

    def test_normal_requests_unaffected(self):
        self.assertEqual(self.client.post(TEXT_URL, {'prompt': 'A cat'}, content_type='application/json').status_code, 202)
        self.assertEqual(self.client.get('/api/v1/generations/', CONTENT_LENGTH=str(LIMIT * 10)).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/health/').status_code, 200)

    def test_missing_content_length_reads_an_empty_body(self):
        response = self.client.generic('POST', TEXT_URL, b'', content_type='application/json', CONTENT_LENGTH='')
        self.assertApiError(response, 400, 'validation_error', fields=['prompt'])
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_malformed_content_length(self):
        response = self.client.post(TEXT_URL, {'prompt': 'A cat'}, content_type='application/json', CONTENT_LENGTH='abc')
        self.assertApiError(response, 400, 'invalid_request')
        self.assertEqual(GenerationJob.objects.count(), 0)


class OversizedRequestsAndRateLimitTests(ThrottleTestCase):
    def test_rejections_do_not_use_the_allowance(self):
        client = self.browser()
        for _ in range(10):  # image uploads: only the guard stops these before parsing
            response = client.post(IMAGE_URL, {'image': SimpleUploadedFile('a.png', PNG, 'image/png')},
                                   headers={'X-CSRFToken': self.token(client)}, CONTENT_LENGTH=str(LIMIT + 1))
            self.assertApiError(response, 413, 'request_too_large')
        self.use_burst(client)  # all five still available
        self.assertThrottled(self.post_text(client))
