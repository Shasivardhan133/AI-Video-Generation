"""Part A: a real DJANGO_SECRET_KEY is required whenever DEBUG is off.

The startup tests run Django settings in a separate process with a controlled
environment. They print only true/false results, never a key.
"""

import os
import subprocess
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from dotenv import dotenv_values

from config.settings import DEVELOPMENT_SECRET_KEY, PLACEHOLDER_SECRET_KEYS, resolve_secret_key

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = BACKEND_DIR.parent / '.env.example'  # the committed example, never the real .env
EXAMPLE_KEY = 'replace-with-a-local-secret'
TEST_KEY = 'test-only-secret-key-4b7f2c9d1e8a6f30a5c2'

STARTUP_PROBE = '''
import os, sys
import dotenv
if os.environ.get("GEETHA_TEST_SKIP_DOTENV"):
    dotenv.load_dotenv = lambda *args, **kwargs: False   # simulate "no .env file"
if os.environ.get("GEETHA_TEST_DOTENV_FILE"):
    real_load = dotenv.load_dotenv                        # simulate ".env copied from this file"
    dotenv.load_dotenv = lambda *args, **kwargs: real_load(os.environ["GEETHA_TEST_DOTENV_FILE"])
from django.conf import settings
try:
    key = settings.SECRET_KEY
except Exception as exc:
    print("ERROR", type(exc).__name__, exc)
    sys.exit(3)
print("OK", key == os.environ.get("GEETHA_EXPECTED_KEY"))
'''


class ResolveSecretKeyTests(SimpleTestCase):
    def test_production_requires_a_key(self):
        for value in (None, '', '   ', '\t\n'):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, 'DJANGO_SECRET_KEY is missing or blank'):
                    resolve_secret_key(value, debug=False)

    def test_production_rejects_both_published_placeholders(self):
        self.assertEqual(PLACEHOLDER_SECRET_KEYS, {DEVELOPMENT_SECRET_KEY, EXAMPLE_KEY})
        for key in (DEVELOPMENT_SECRET_KEY, EXAMPLE_KEY, f'  {EXAMPLE_KEY}  ', f'{DEVELOPMENT_SECRET_KEY}\n'):
            with self.subTest(key=key):
                with self.assertRaisesMessage(ImproperlyConfigured, 'published placeholder value') as ctx:
                    resolve_secret_key(key, debug=False)
                self.assertNotIn(key.strip(), str(ctx.exception))

    def test_env_example_key_is_rejected_in_production(self):
        example = dotenv_values(ENV_EXAMPLE)
        self.assertEqual(example['DJANGO_SECRET_KEY'], EXAMPLE_KEY)
        with self.assertRaises(ImproperlyConfigured):
            resolve_secret_key(example['DJANGO_SECRET_KEY'], debug=False)

    def test_placeholders_still_work_in_debug(self):
        for key in PLACEHOLDER_SECRET_KEYS:
            with self.subTest(key=key):
                self.assertEqual(resolve_secret_key(key, debug=True), key)

    def test_production_key_used_exactly_as_configured(self):
        self.assertEqual(resolve_secret_key(TEST_KEY, debug=False), TEST_KEY)
        self.assertEqual(resolve_secret_key('  key with spaces  ', debug=False), '  key with spaces  ')

    def test_development_fallback_only_in_debug(self):
        for value in (None, '', '   '):
            with self.subTest(value=value):
                self.assertEqual(resolve_secret_key(value, debug=True), DEVELOPMENT_SECRET_KEY)
        self.assertEqual(resolve_secret_key(TEST_KEY, debug=True), TEST_KEY)


class StartupTests(SimpleTestCase):
    def start(self, debug=None, key=..., skip_dotenv=False, dotenv_file=None, expected=None):
        env = {k: v for k, v in os.environ.items() if k not in ('DJANGO_SECRET_KEY', 'DJANGO_DEBUG')}
        env.update(DJANGO_SETTINGS_MODULE='config.settings', GEETHA_EXPECTED_KEY=expected or '')
        if debug is not None:
            env['DJANGO_DEBUG'] = 'True' if debug else 'False'
        if key is not ...:
            env['DJANGO_SECRET_KEY'] = key  # set (even blank) values win over .env
        if skip_dotenv:
            env['GEETHA_TEST_SKIP_DOTENV'] = '1'
        if dotenv_file:
            env['GEETHA_TEST_DOTENV_FILE'] = str(dotenv_file)
        result = subprocess.run([sys.executable, '-c', STARTUP_PROBE], cwd=BACKEND_DIR, env=env,
                                capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout + result.stderr

    def test_missing_key_fails_startup(self):
        code, output = self.start(debug=False, skip_dotenv=True)
        self.assertEqual(code, 3, output)
        self.assertIn('ImproperlyConfigured DJANGO_SECRET_KEY is missing or blank', output)

    def test_blank_key_fails_startup(self):
        for key in ('', '   '):
            with self.subTest(key=key):
                code, output = self.start(debug=False, key=key)
                self.assertEqual(code, 3, output)
                self.assertIn('DJANGO_SECRET_KEY is missing or blank', output)

    def test_placeholder_keys_fail_startup(self):
        for key in (DEVELOPMENT_SECRET_KEY, EXAMPLE_KEY):
            with self.subTest(key=key):
                code, output = self.start(debug=False, key=key)
                self.assertEqual(code, 3, output)
                self.assertIn('published placeholder value', output)
                self.assertNotIn(key, output)

    def test_unchanged_env_example_fails_startup(self):
        # As if .env.example were copied to .env without editing (DJANGO_DEBUG=False in it).
        code, output = self.start(dotenv_file=ENV_EXAMPLE)
        self.assertEqual(code, 3, output)
        self.assertIn('published placeholder value', output)

    def test_unchanged_env_example_starts_in_debug(self):
        code, output = self.start(debug=True, dotenv_file=ENV_EXAMPLE, expected=EXAMPLE_KEY)
        self.assertEqual((code, output.strip()), (0, 'OK True'))

    def test_valid_production_key_starts_and_is_not_printed(self):
        code, output = self.start(debug=False, key=TEST_KEY, expected=TEST_KEY)
        self.assertEqual((code, output.strip()), (0, 'OK True'))
        self.assertNotIn(TEST_KEY, output)

    def test_debug_falls_back_to_the_development_key(self):
        code, output = self.start(debug=True, key='', expected=DEVELOPMENT_SECRET_KEY)
        self.assertEqual((code, output.strip()), (0, 'OK True'))
        code, output = self.start(debug=True, skip_dotenv=True, expected=DEVELOPMENT_SECRET_KEY)
        self.assertEqual((code, output.strip()), (0, 'OK True'))


class AIServiceModeTests(SimpleTestCase):
    """AI_SERVICE_MODE: 'mock' (default) or 'remote'; a misspelt mode stops startup
    instead of silently producing mock test videos."""

    def test_valid_and_default_modes(self):
        from config.settings import resolve_ai_service_mode
        cases = {None: 'mock', '': 'mock', '   ': 'mock', 'mock': 'mock', ' Remote\n': 'remote', 'REMOTE': 'remote'}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(resolve_ai_service_mode(raw), expected)

    def test_unknown_modes_are_refused(self):
        from config.settings import resolve_ai_service_mode
        for raw in ('colab', 'remot', 'real', 'mock,remote', '0'):
            with self.subTest(raw=raw):
                with self.assertRaisesMessage(ImproperlyConfigured, "AI_SERVICE_MODE must be 'mock' or 'remote'"):
                    resolve_ai_service_mode(raw)
