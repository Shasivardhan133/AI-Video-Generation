"""Shared helpers. Every test that can touch files uses a throwaway MEDIA_ROOT."""

import base64
import shutil
import struct
import tempfile
import zlib
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.generator.models import GenerationJob

REAL_MEDIA_ROOT = Path(settings.MEDIA_ROOT).resolve()

# Real, decodable 2x3-pixel images (made once with FFmpeg), so tests do not rely
# on fake signatures. The three WebP files cover the VP8, VP8L and VP8X layouts.
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAIAAAADCAIAAAA2iEnWAAAACXBIWXMAAAABAAAAAQBPJcTWAAAAEElEQVR4nGMwSD0JRAwoFABSoQg1'
    'oAWlQQAAAABJRU5ErkJggg==')
JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYxLjE5LjEwMAD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYGBgYHBwcICAgHBwcG'
    'BgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABLAAEBAAAAAAAAAAAAAAAAAAAABQEBAAAAAAAAAAAAAAAAAAAABhABAAAAAAAA'
    'AAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIAAMAAgMBIgACEQADEQD/2gAMAwEAAhEDEQA/AIIB2Ov/2Q==')
WEBP = base64.b64decode('UklGRjYAAABXRUJQVlA4ICoAAADQAQCdASoCAAMAAgA0JaACdLoB+AADsAD+7Qcv/PTNeYP8nP+3Jl5tsAA=')
WEBP_LOSSLESS = base64.b64decode('UklGRh4AAABXRUJQVlA4TBEAAAAvAYAAAAdQs8p0uf+BiOh/AAA=')
WEBP_EXTENDED = base64.b64decode(
    'UklGRlgAAABXRUJQVlA4WAoAAAAQAAAAAQAAAgAAQUxQSAcAAAAAf39/f39/AFZQOCAqAAAA0AEAnQEqAgADAAIANCWgAnS6AfgAA7AA'
    '/u0HL/z0zXmD/Jz/tyZebbAA')
GIF = b'GIF89a' + b'\x00' * 64


def _png_chunk(chunk_type, data):
    return struct.pack('>I', len(data)) + chunk_type + data + struct.pack('>I', zlib.crc32(chunk_type + data))


def make_png(width=2, height=3, total_size=None):
    """A structurally valid PNG declaring width x height. With total_size, a
    private ancillary chunk pads the file to exactly that many bytes. Pixel data
    is a single tiny IDAT, so large declared sizes stay small on disk."""
    head = b'\x89PNG\r\n\x1a\n' + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    idat = _png_chunk(b'IDAT', zlib.compress(b'\x00' * 7))
    iend = _png_chunk(b'IEND', b'')
    padding = b''
    if total_size is not None:
        padding = _png_chunk(b'paDd', b'\x00' * (total_size - len(head) - len(idat) - len(iend) - 12))
    return head + idat + padding + iend
# Smallest content the API treats as a valid MP4: an 'ftyp' box and at least 1 KB.
MP4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 2048

TEXT_SETTINGS = {
    'model_profile': 'wan2.1-t2v-1.3b-480p', 'width': 832, 'height': 480,
    'frame_count': 81, 'fps': 16, 'seed': None, 'style': None, 'enhance_prompt': False,
}
IMAGE_SETTINGS = {**TEXT_SETTINGS, 'model_profile': 'wan2.1-i2v-14b-480p'}


# Regression tests post many times from one address (127.0.0.1), so by default
# they run with high generation limits. The throttle classes still run; the real
# limits are tested in tests/test_throttling.py (relax_generation_limits = False).
RELAXED_THROTTLE_RATES = {'generation_burst': '1000/minute', 'generation_sustained': '1000/hour'}
# Likewise, without a worker every job a test creates stays queued, so tests about
# other features would hit the one-active-job rule on their second request. They
# run with a high limit; the lock and the active-job check still run on every
# create. The real limit (1) is tested in tests/test_active_jobs.py
# (relax_active_job_limit = False).
RELAXED_ACTIVE_JOB_LIMIT = 1000


class TempMediaMixin:
    """Test setup whose MEDIA_ROOT is a temporary folder, deleted afterwards."""

    relax_generation_limits = True
    relax_active_job_limit = True

    def setUp(self):
        super().setUp()
        # Throttle counters live in the cache; never let them leak between tests.
        cache.clear()
        self.addCleanup(cache.clear)
        if self.relax_generation_limits:
            relaxed = override_settings(REST_FRAMEWORK={
                **settings.REST_FRAMEWORK, 'DEFAULT_THROTTLE_RATES': RELAXED_THROTTLE_RATES,
            })
            relaxed.enable()
            self.addCleanup(relaxed.disable)
        if self.relax_active_job_limit:
            limit = override_settings(GENERATION_MAX_ACTIVE_JOBS_PER_OWNER=RELAXED_ACTIVE_JOB_LIMIT)
            limit.enable()
            self.addCleanup(limit.disable)
        self.media_root = Path(tempfile.mkdtemp(prefix='geetha-test-media-'))
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=str(self.media_root))
        media.enable()
        self.addCleanup(media.disable)
        # Refuse to run if storage still points at the real media folder.
        # Explicit checks, not `assert`: they must also hold under `python -O`.
        location = Path(default_storage.location).resolve()
        if location != self.media_root.resolve() or location == REAL_MEDIA_ROOT:
            raise RuntimeError(f'Refusing to run: storage points at {location}, not the temporary media folder')

    def session_key(self, client=None):
        """Start (or reuse) the test client's session and return its key."""
        return (client or self.client).session.session_key

    def make_job(self, session_key='', minutes_ago=0, **fields):
        mode = fields.pop('mode', GenerationJob.Mode.TEXT_TO_VIDEO)
        defaults = {
            'session_key': session_key,
            'mode': mode,
            'prompt_text': 'A lighthouse at dusk',
            'settings': dict(IMAGE_SETTINGS if mode == GenerationJob.Mode.IMAGE_TO_VIDEO else TEXT_SETTINGS),
            'created_at': timezone.now() - timedelta(minutes=minutes_ago),
        }
        defaults.update(fields)
        return GenerationJob.objects.create(**defaults)

    def make_completed_job(self, session_key='', remote_job_id='mock-x', content=MP4, **fields):
        job = self.make_job(session_key, status=GenerationJob.Status.COMPLETED,
                            remote_job_id=remote_job_id, completed_at=timezone.now(), **fields)
        job.output_file.save(f'{job.id}.mp4', ContentFile(content), save=True)
        return job

    def assertApiError(self, response, status, code, fields=None):
        """Every API error uses {"error": {"code", "message", "request_id", ["fields"]}}."""
        self.assertEqual(response.status_code, status, response.content[:500])
        body = response.json()
        self.assertEqual(set(body), {'error'})
        error = body['error']
        expected_keys = {'code', 'message', 'request_id'} | ({'fields'} if fields is not None else set())
        self.assertEqual(set(error), expected_keys)
        self.assertEqual(error['code'], code)
        self.assertIsInstance(error['message'], str)
        self.assertTrue(error['message'])
        self.assertEqual(error['request_id'], response['X-Request-ID'])
        if fields is not None:
            self.assertEqual(set(error['fields']), set(fields))
        return error


class TempMediaTestCase(TempMediaMixin, TestCase):
    """Each test runs in a transaction that is rolled back."""


class TempMediaTransactionTestCase(TempMediaMixin, TransactionTestCase):
    """For tests that use several threads/database connections (committed data,
    tables emptied after each test). Still the temporary test database only."""
