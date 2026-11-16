"""Rate limits on the two generation endpoints, using the real configured limits.

Every client enforces CSRF, like a real browser, and has an explicit client
address. Time is controlled with a fake clock, so no test waits in real time.
"""

import re
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from rest_framework.settings import api_settings

from apps.generator.models import GenerationJob
from apps.generator.throttles import GenerationRateThrottle

from .base import PNG, TempMediaTestCase

TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'
CONFIGURED_RATES = {'generation_burst': '5/minute', 'generation_sustained': '30/hour'}


class FakeClock:
    """Callable stand-in for time.time (an instance, so it is not bound as a method)."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ThrottleTestCase(TempMediaTestCase):
    relax_generation_limits = False  # use the real limits from settings.py

    def setUp(self):
        super().setUp()
        self.assertEqual(settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'], CONFIGURED_RATES)
        self.assertEqual(api_settings.DEFAULT_THROTTLE_RATES, CONFIGURED_RATES)
        self.clock = FakeClock()
        patcher = mock.patch.object(GenerationRateThrottle, 'timer', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tokens = {}

    def browser(self, ip='10.0.0.1'):
        return Client(enforce_csrf_checks=True, REMOTE_ADDR=ip)

    def token(self, client):
        if client not in self.tokens:
            self.tokens[client] = client.get('/api/v1/csrf/').json()['csrf_token']
        return self.tokens[client]

    def post_text(self, client, prompt='A lighthouse at dusk', **extra):
        return client.post(TEXT_URL, {'prompt': prompt}, content_type='application/json',
                           headers={'X-CSRFToken': self.token(client)}, **extra)

    def post_image(self, client, **extra):
        return client.post(IMAGE_URL, {'image': SimpleUploadedFile('p.png', PNG, content_type='image/png')},
                           headers={'X-CSRFToken': self.token(client)}, **extra)

    def accept(self, response):
        self.assertEqual(response.status_code, 202, response.content[:300])

    def assertThrottled(self, response, min_wait=1, max_wait=3600):
        error = self.assertApiError(response, 429, 'rate_limited')
        wait = int(response['Retry-After'])
        self.assertGreaterEqual(wait, min_wait)
        self.assertLessEqual(wait, max_wait)
        self.assertRegex(error['message'], r'^Too many video requests\. Please wait \d+ seconds? before trying again\.$')
        self.assertEqual(int(re.search(r'\d+', error['message']).group()), wait)
        return wait

    def use_burst(self, client, count=5):
        for _ in range(count):
            self.accept(self.post_text(client))


class ConfiguredLimitTests(ThrottleTestCase):
    def test_five_accepted_then_sixth_rejected(self):
        client = self.browser()
        self.use_burst(client)
        self.assertThrottled(self.post_text(client), max_wait=60)
        self.assertEqual(GenerationJob.objects.count(), 5)

    def test_429_response_shape(self):
        client = self.browser()
        self.use_burst(client)
        response = self.post_text(client)
        self.assertEqual(response['Content-Type'], 'application/json')
        body = response.json()
        self.assertEqual(set(body['error']), {'code', 'message', 'request_id'})
        self.assertEqual(body['error']['request_id'], response['X-Request-ID'])
        self.assertEqual(self.assertThrottled(response), 60)

    def test_text_and_image_share_the_burst_allowance(self):
        client = self.browser()
        for post in (self.post_text, self.post_text, self.post_text, self.post_image, self.post_image):
            self.accept(post(client))
        self.assertThrottled(self.post_text(client), max_wait=60)
        self.assertThrottled(self.post_image(client), max_wait=60)
        self.assertEqual(GenerationJob.objects.count(), 5)

    def test_throttled_image_request_stores_nothing(self):
        client = self.browser()
        self.use_burst(client)
        self.assertThrottled(self.post_image(client))
        self.assertEqual(GenerationJob.objects.filter(mode=GenerationJob.Mode.IMAGE_TO_VIDEO).count(), 0)
        self.assertFalse((self.media_root / 'inputs').exists())

    def test_burst_window_reopens_after_a_minute(self):
        client = self.browser()
        self.use_burst(client)
        self.clock.advance(59)
        self.assertThrottled(self.post_text(client), max_wait=1)
        self.clock.advance(2)
        self.accept(self.post_text(client))

    def test_hourly_limit_on_its_own(self):
        # Six spaced batches of five never hit the burst limit; the 31st request
        # in the hour is blocked by the hourly limit (wait far above a minute).
        client = self.browser()
        posts = [self.post_text, self.post_image]
        for batch in range(6):
            for i in range(5):
                self.accept(posts[(batch + i) % 2](client))
            self.clock.advance(61)
        self.assertEqual(GenerationJob.objects.count(), 30)
        self.assertEqual(GenerationJob.objects.filter(mode=GenerationJob.Mode.IMAGE_TO_VIDEO).count(), 15)
        wait = self.assertThrottled(self.post_text(client), min_wait=61)
        self.assertThrottled(self.post_image(client), min_wait=61)
        self.assertEqual(GenerationJob.objects.count(), 30)

        self.clock.advance(wait)
        self.accept(self.post_text(client))

    def test_burst_rejections_still_count_toward_the_hourly_limit(self):
        # Stock DRF behaviour: every throttle records a request it allows, even
        # when another throttle rejects it. Documented in the API docs.
        client = self.browser()
        self.use_burst(client)
        for _ in range(3):
            self.assertThrottled(self.post_text(client), max_wait=60)
        accepted = 5
        for _ in range(6):
            self.clock.advance(61)
            for _ in range(5):
                if self.post_text(client).status_code == 202:
                    accepted += 1
        self.assertEqual(accepted, 27)  # 30 - 3 burst-rejected requests


class IdentityTests(ThrottleTestCase):
    def test_different_ips_have_separate_allowances(self):
        first, second = self.browser('10.0.0.1'), self.browser('10.0.0.2')
        self.use_burst(first)
        self.assertThrottled(self.post_text(first))
        self.use_burst(second)
        self.assertThrottled(self.post_text(second))

    def test_clearing_cookies_does_not_reset_a_guest(self):
        client = self.browser()
        self.use_burst(client)
        client.cookies.clear()
        self.tokens.pop(client)
        self.assertThrottled(self.post_text(client))
        self.assertEqual(GenerationJob.objects.count(), 5)

    def test_new_guest_session_on_same_ip_shares_the_allowance(self):
        self.use_burst(self.browser('10.0.0.1'))
        self.assertThrottled(self.post_text(self.browser('10.0.0.1')))

    def test_signed_in_users_on_one_ip_have_separate_allowances(self):
        users = get_user_model().objects
        alice = self.browser('10.0.0.1')
        alice.force_login(users.create_user('alice', password='unused-test-password'))
        bob = self.browser('10.0.0.1')
        bob.force_login(users.create_user('bob', password='unused-test-password'))
        self.use_burst(alice)
        self.assertThrottled(self.post_text(alice))
        self.use_burst(bob)
        self.assertThrottled(self.post_text(bob))
        # A guest on the same address still has its own allowance.
        self.use_burst(self.browser('10.0.0.1'))

    def test_forged_forwarded_for_header_is_ignored(self):
        client = self.browser()
        for i in range(5):
            self.accept(self.post_text(client, HTTP_X_FORWARDED_FOR=f'203.0.113.{i}'))
        self.assertThrottled(self.post_text(client, HTTP_X_FORWARDED_FOR='198.51.100.7'))
        self.assertThrottled(self.post_text(client, HTTP_X_FORWARDED_FOR='10.0.0.2'))


class OtherMethodTests(ThrottleTestCase):
    """Only POST creates jobs, so only POST may use the generation allowance."""

    def assertMethodNotAllowed(self, response, has_body=True):
        if has_body:
            self.assertApiError(response, 405, 'method_not_allowed')
        else:  # HEAD responses carry no body
            self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Allow'], 'POST, OPTIONS')

    def test_repeated_get_and_head_do_not_use_the_allowance(self):
        client = self.browser()
        for _ in range(10):
            for url in (TEXT_URL, IMAGE_URL):
                self.assertMethodNotAllowed(client.get(url))
                self.assertMethodNotAllowed(client.head(url), has_body=False)
        self.use_burst(client)  # all five still accepted
        self.assertThrottled(self.post_text(client), max_wait=60)

    def test_repeated_options_do_not_use_the_allowance(self):
        client = self.browser()
        for _ in range(10):
            for url in (TEXT_URL, IMAGE_URL):
                response = client.options(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Allow'], 'POST, OPTIONS')
        self.use_burst(client)
        self.assertThrottled(self.post_text(client), max_wait=60)

    def test_other_unsafe_methods_still_need_csrf_and_do_not_use_the_allowance(self):
        client = self.browser()
        headers = {'X-CSRFToken': self.token(client)}
        for _ in range(5):
            for method in (client.put, client.patch, client.delete):
                self.assertApiError(method(TEXT_URL), 403, 'forbidden')  # no token: CSRF first
                self.assertMethodNotAllowed(method(TEXT_URL, headers=headers))
        self.use_burst(client)
        self.assertThrottled(self.post_text(client), max_wait=60)

    def test_wrong_methods_still_get_405_after_the_limit(self):
        client = self.browser()
        self.use_burst(client)
        self.assertThrottled(self.post_text(client))
        for _ in range(5):
            self.assertMethodNotAllowed(client.get(TEXT_URL))
            self.assertMethodNotAllowed(client.get(IMAGE_URL))
            self.assertEqual(client.options(TEXT_URL).status_code, 200)
        self.assertThrottled(self.post_image(client))  # POST is still limited
        self.assertEqual(GenerationJob.objects.count(), 5)


class RequestProcessingTests(ThrottleTestCase):
    def test_csrf_rejections_do_not_use_the_allowance(self):
        client = self.browser()
        for _ in range(10):
            response = client.post(TEXT_URL, {'prompt': 'a'}, content_type='application/json')
            self.assertApiError(response, 403, 'forbidden')
        self.use_burst(client)
        self.assertThrottled(self.post_text(client))

    def test_invalid_requests_use_the_allowance(self):
        client = self.browser()
        for _ in range(5):
            self.assertApiError(self.post_text(client, prompt=''), 400, 'validation_error', fields=['prompt'])
        self.assertThrottled(self.post_text(client))
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_csrf_still_checked_before_the_throttle(self):
        client = self.browser()
        self.use_burst(client)
        response = client.post(TEXT_URL, {'prompt': 'a'}, content_type='application/json')
        self.assertApiError(response, 403, 'forbidden')

    def test_reads_keep_working_after_the_limit(self):
        client = self.browser()
        self.use_burst(client)
        self.assertThrottled(self.post_text(client))
        done = self.make_completed_job(self.session_key(client))

        self.assertEqual(client.get('/api/v1/csrf/').status_code, 200)
        self.assertEqual(client.get('/api/v1/health/').status_code, 200)
        self.assertEqual(client.get('/api/v1/profiles/').status_code, 200)
        history = client.get('/api/v1/generations/', {'page_size': 30})
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()['count'], 6)
        self.assertEqual(client.get('/api/v1/generations/', {'page': 1, 'page_size': 2}).status_code, 200)
        job_id = history.json()['results'][-1]['job_id']
        for _ in range(30):  # status polling every few seconds
            self.assertEqual(client.get(f'/api/v1/generations/{job_id}/').status_code, 200)
        for params in ({}, {'download': '1'}):
            response = client.get(f'/api/v1/generations/{done.id}/video/', params)
            self.assertEqual(response.status_code, 200)
            b''.join(response.streaming_content)  # let the test client close the file

    def test_ownership_and_sessions_unchanged(self):
        owner = self.browser()
        self.use_burst(owner)
        self.assertEqual(Session.objects.count(), 1)

        # A new visitor on the same address is throttled, but no session is created
        # and they cannot see the owner's jobs.
        stranger = self.browser()
        self.assertThrottled(self.post_text(stranger))
        self.assertEqual(Session.objects.count(), 1)
        self.assertEqual(stranger.get('/api/v1/generations/').json()['count'], 0)
        job = GenerationJob.objects.first()
        self.assertApiError(stranger.get(f'/api/v1/generations/{job.id}/'), 404, 'not_found')
        self.assertEqual(owner.get('/api/v1/generations/').json()['count'], 5)


class ThrottledResponsePrivacyTests(ThrottleTestCase):
    def test_rate_limited_response_is_private(self):
        client = self.browser()
        for _ in range(5):
            self.accept(self.post_text(client))
        limited = self.post_text(client)
        self.assertApiError(limited, 429, 'rate_limited')
        self.assertEqual(limited['Cache-Control'], 'private, no-store')
        self.assertIn('Cookie', limited['Vary'])
        self.assertIn('Retry-After', limited)
