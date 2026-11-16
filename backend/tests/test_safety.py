"""Guards: the suite must never run against the real database or media folder."""

from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import connection
from django.test import SimpleTestCase

from .base import REAL_MEDIA_ROOT, TempMediaTestCase


class IsolationTests(TempMediaTestCase):
    def test_uses_temporary_test_database(self):
        name = connection.settings_dict['NAME']
        self.assertTrue(name.startswith('test_'), name)
        self.assertNotEqual(name, 'ai_video_generator')

    def test_uses_temporary_media_root(self):
        self.assertEqual(Path(settings.MEDIA_ROOT).resolve(), self.media_root.resolve())
        self.assertNotEqual(Path(default_storage.location).resolve(), REAL_MEDIA_ROOT)


class NoRealAIServiceTests(SimpleTestCase):
    """The real .env may hold a Colab URL and API key; tests must never be able to use them,
    whatever AI_SERVICE_MODE the .env sets (see tests/__init__.py)."""

    def test_mock_mode_and_no_real_service_or_key(self):
        self.assertEqual(settings.AI_SERVICE_MODE, 'mock')
        self.assertEqual(settings.AI_SERVICE_API_KEY, '')
        self.assertTrue(settings.AI_SERVICE_BASE_URL.endswith('.invalid'), 'a real AI service URL is configured')

    def test_the_default_client_is_the_local_mock(self):
        from ai_service.client import get_client
        from ai_service.client.mock import MockClient
        self.assertIsInstance(get_client(settings), MockClient)


class MediaGuardTests(SimpleTestCase):
    def test_setup_refuses_when_storage_points_at_the_real_media_folder(self):
        # Only paths are compared; the real folder is never read or written.
        class Probe(TempMediaTestCase):
            def runTest(self):
                pass

        probe = Probe()
        fake_storage = mock.Mock(location=str(REAL_MEDIA_ROOT))
        with mock.patch('tests.base.default_storage', fake_storage), \
                self.assertRaisesMessage(RuntimeError, 'Refusing to run'):
            probe.setUp()
        probe.doCleanups()  # undo the setting overrides setUp had enabled
