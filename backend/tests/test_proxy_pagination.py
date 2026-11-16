"""C10: absolute history pagination links behind an HTTPS reverse proxy.

`next` and `previous` are absolute URLs built from the request. X-Forwarded-Proto
is trusted only when DJANGO_BEHIND_HTTPS_PROXY is on (SECURE_PROXY_SSL_HEADER);
otherwise a client-supplied value is ignored. X-Forwarded-Host is never trusted.
"""

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from django.test import Client, SimpleTestCase, override_settings
from django.utils import timezone

from apps.generator.models import GenerationJob

from .base import TempMediaTestCase

LIST_URL = '/api/v1/generations/'
PROXY_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
BACKEND_DIR = Path(__file__).resolve().parent.parent

SETTINGS_PROBE = '''
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False   # ignore any real .env file
from django.conf import settings
print(repr(settings.SECURE_PROXY_SSL_HEADER), settings.USE_X_FORWARDED_HOST)
'''


class ProxyPaginationTests(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.session_key()
        now = timezone.now()
        GenerationJob.objects.bulk_create([
            GenerationJob(session_key=self.mine, mode=GenerationJob.Mode.TEXT_TO_VIDEO, prompt_text=str(i), settings={},
                          created_at=now - timedelta(minutes=i))
            for i in range(12)
        ])
        GenerationJob.objects.bulk_create([  # another visitor's jobs never count
            GenerationJob(session_key=self.session_key(Client()), mode=GenerationJob.Mode.TEXT_TO_VIDEO, settings={})
            for _ in range(5)
        ])

    def page(self, page=2, secure=False, **headers):
        # Browsers always send Host, and the proxy must pass it on unchanged.
        headers.setdefault('Host', 'testserver')
        response = self.client.get(LIST_URL, {'page_size': 5, 'page': page}, secure=secure, headers=headers)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_plain_http(self):
        body = self.page()
        self.assertEqual(body['count'], 12)
        self.assertEqual(body['next'], 'http://testserver/api/v1/generations/?page=3&page_size=5')
        self.assertEqual(body['previous'], 'http://testserver/api/v1/generations/?page_size=5')

    def test_direct_https(self):
        body = self.page(secure=True)
        self.assertEqual(body['next'], 'https://testserver/api/v1/generations/?page=3&page_size=5')
        self.assertEqual(body['previous'], 'https://testserver/api/v1/generations/?page_size=5')

    def test_forwarded_proto_is_ignored_without_a_trusted_proxy(self):
        body = self.page(**{'X-Forwarded-Proto': 'https'})
        self.assertEqual(body['next'], 'http://testserver/api/v1/generations/?page=3&page_size=5')

    @override_settings(SECURE_PROXY_SSL_HEADER=PROXY_HEADER)
    def test_trusted_proxy_https(self):
        body = self.page(**{'X-Forwarded-Proto': 'https'})
        self.assertEqual(body['next'], 'https://testserver/api/v1/generations/?page=3&page_size=5')
        self.assertEqual(body['previous'], 'https://testserver/api/v1/generations/?page_size=5')

    @override_settings(SECURE_PROXY_SSL_HEADER=PROXY_HEADER)
    def test_trusted_proxy_plain_http_or_missing_header(self):
        for headers in ({'X-Forwarded-Proto': 'http'}, {}, {'X-Forwarded-Proto': 'HTTPS-ish'}):
            with self.subTest(headers=headers):
                body = self.page(**headers)
                self.assertEqual(body['next'], 'http://testserver/api/v1/generations/?page=3&page_size=5')

    @override_settings(SECURE_PROXY_SSL_HEADER=PROXY_HEADER, ALLOWED_HOSTS=['testserver'])
    def test_forwarded_host_is_never_used(self):
        body = self.page(**{'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'evil.example'})
        self.assertEqual(body['next'], 'https://testserver/api/v1/generations/?page=3&page_size=5')

    @override_settings(SECURE_PROXY_SSL_HEADER=PROXY_HEADER, ALLOWED_HOSTS=['geetha.example'])
    def test_public_host_name_from_the_proxy(self):
        body = self.page(Host='geetha.example', **{'X-Forwarded-Proto': 'https'})
        self.assertEqual(body['next'], 'https://geetha.example/api/v1/generations/?page=3&page_size=5')
        # A Host outside ALLOWED_HOSTS is still refused, proxy or not.
        response = self.client.get(LIST_URL, headers={'Host': 'evil.example', 'X-Forwarded-Proto': 'https'})
        self.assertEqual(response.status_code, 400)

    @override_settings(SECURE_PROXY_SSL_HEADER=PROXY_HEADER)
    def test_pages_stay_complete_and_ordered(self):
        seen = []
        url = f'{LIST_URL}?page_size=5'
        while url:
            response = self.client.get(url, headers={'X-Forwarded-Proto': 'https', 'Host': 'testserver'})
            body = response.json()
            seen += [item['job_id'] for item in body['results']]
            url = body['next']
            if url:
                self.assertTrue(url.startswith('https://testserver/'), url)
        expected = list(GenerationJob.objects.filter(session_key=self.mine).order_by('-created_at')
                        .values_list('id', flat=True))
        self.assertEqual(len(seen), 12)
        self.assertEqual(seen, [str(job_id) for job_id in expected])

    def test_first_and_last_pages(self):
        first = self.page(page=1)
        self.assertIsNone(first['previous'])
        self.assertEqual(first['next'], 'http://testserver/api/v1/generations/?page=2&page_size=5')
        last = self.page(page=3)
        self.assertIsNone(last['next'])
        self.assertEqual(len(last['results']), 2)

    @override_settings(SECURE_PROXY_SSL_HEADER=PROXY_HEADER)
    def test_https_csrf_origin_accepted_only_through_the_trusted_proxy(self):
        browser = Client(enforce_csrf_checks=True)
        token = browser.get('/api/v1/csrf/').json()['csrf_token']
        post = lambda **headers: browser.post('/api/v1/generations/text-to-video/', {'prompt': 'A calm lake'},
                                              content_type='application/json',
                                              headers={'X-CSRFToken': token, 'Origin': 'https://testserver',
                                                       'Host': 'testserver', **headers})
        self.assertEqual(post(**{'X-Forwarded-Proto': 'https'}).status_code, 202)
        self.assertEqual(post().status_code, 403)  # no proxy header: an http request from an https origin


class ProxySettingTests(SimpleTestCase):
    def settings_with(self, value=None):
        env = {k: v for k, v in os.environ.items() if k != 'DJANGO_BEHIND_HTTPS_PROXY'}
        env.update(DJANGO_SETTINGS_MODULE='config.settings', DJANGO_DEBUG='True')
        if value is not None:
            env['DJANGO_BEHIND_HTTPS_PROXY'] = value
        result = subprocess.run([sys.executable, '-c', SETTINGS_PROBE], cwd=BACKEND_DIR, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_off_by_default(self):
        self.assertEqual(self.settings_with(), 'None False')
        for value in ('False', '0', '', 'no', 'https'):
            with self.subTest(value=value):
                self.assertEqual(self.settings_with(value), 'None False')

    def test_enabled(self):
        for value in ('True', 'true', '1', 'yes'):
            with self.subTest(value=value):
                self.assertEqual(self.settings_with(value), "('HTTP_X_FORWARDED_PROTO', 'https') False")
