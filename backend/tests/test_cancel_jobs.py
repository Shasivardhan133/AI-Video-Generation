"""3.5.4 (B): POST /api/v1/generations/{id}/cancel/ cancels the caller's own queued job.

A cancelled job is `failed` with error code `cancelled` (no new status, no
migration). Processing and finished jobs cannot be cancelled (409
`not_cancellable`); other owners' jobs look missing (404). Nothing is deleted.
"""

import logging
import threading
import time
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.test import Client, override_settings
from django.utils import timezone

from apps.generator.models import GenerationJob
from apps.generator.worker import claim_next_job

from .base import PNG, TempMediaTestCase, TempMediaTransactionTestCase

S = GenerationJob.Status
TEXT_URL = '/api/v1/generations/text-to-video/'
CANCELLED_MESSAGE = 'You cancelled this video.'


def cancel_url(job_or_id):
    return f'/api/v1/generations/{getattr(job_or_id, "pk", job_or_id)}/cancel/'


def stored(job):
    return GenerationJob.objects.filter(pk=job.pk).values().get()


class CancelHelpers:
    def setUp(self):
        super().setUp()
        self.mine = self.session_key()
        # Each cancellation is logged at INFO; keep it off the test console.
        quiet = mock.patch.object(logging.getLogger('apps'), 'handlers', [logging.NullHandler()])
        quiet.start()
        self.addCleanup(quiet.stop)

    def cancel(self, job, client=None):
        return (client or self.client).post(cancel_url(job))

    def assertCancelled(self, response, job):
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        body = response.json()
        self.assertEqual((body['job_id'], body['status'], body['result']), (str(job.pk), 'failed', None))
        self.assertEqual((body['error']['code'], body['error']['message']), ('cancelled', CANCELLED_MESSAGE))
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code, job.error_message), (S.FAILED, 'cancelled', CANCELLED_MESSAGE))
        self.assertIsNotNone(job.completed_at)
        self.assertIsNone(job.started_at)

    def assertNotCancellable(self, response, message):
        error = self.assertApiError(response, 409, 'not_cancellable')
        self.assertEqual(error['message'], message)
        self.assertEqual(response['Cache-Control'], 'private, no-store')


class CancelQueuedJobTests(CancelHelpers, TempMediaTestCase):
    def test_owner_cancels_a_queued_job(self):
        job = self.make_job(self.mine)
        self.assertCancelled(self.cancel(job), job)
        self.assertIsNone(claim_next_job())  # a worker never starts it

    def test_cancelling_keeps_the_input_image_and_other_media(self):
        other = self.make_completed_job(self.mine)
        job = self.make_job(self.mine, mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        job.input_file.save('in.png', ContentFile(PNG), save=True)
        files_before = sorted(p for p in self.media_root.rglob('*') if p.is_file())
        self.assertCancelled(self.cancel(job), job)
        self.assertEqual(job.input_file.name, GenerationJob.objects.get(pk=job.pk).input_file.name)
        self.assertEqual(sorted(p for p in self.media_root.rglob('*') if p.is_file()), files_before)
        self.assertEqual(stored(other)['status'], S.COMPLETED)

    def test_cancelling_again_is_harmless(self):
        job = self.make_job(self.mine)
        self.assertCancelled(self.cancel(job), job)
        before = stored(job)
        self.assertCancelled(self.cancel(job), job)
        self.assertEqual(stored(job), before)  # unchanged, including completed_at

    def test_processing_and_finished_jobs_cannot_be_cancelled(self):
        cases = [
            (self.make_job(self.mine, status=S.PROCESSING, started_at=timezone.now()),
             'This video is already being generated and can no longer be cancelled.'),
            (self.make_completed_job(self.mine), 'This video has already finished.'),
            (self.make_job(self.mine, status=S.FAILED, error_code='queue_timeout'), 'This video has already stopped.'),
            (self.make_job(self.mine, status=S.FAILED, error_code='service_timeout'), 'This video has already stopped.'),
        ]
        for job, message in cases:
            with self.subTest(status=job.status, code=job.error_code):
                before = stored(job)
                self.assertNotCancellable(self.cancel(job), message)
                self.assertEqual(stored(job), before)

    def test_completed_video_is_untouched(self):
        job = self.make_completed_job(self.mine)
        path = self.media_root / job.output_file.name
        content = path.read_bytes()
        self.cancel(job)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(GenerationJob.objects.get(pk=job.pk).output_file.name, job.output_file.name)

    def test_cancelled_job_is_reported_by_detail_and_history(self):
        job = self.make_job(self.mine)
        self.cancel(job)
        detail = self.client.get(f'/api/v1/generations/{job.pk}/').json()
        self.assertEqual((detail['status'], detail['error']['code']), ('failed', 'cancelled'))
        item = self.client.get('/api/v1/generations/').json()['results'][0]
        self.assertEqual((item['job_id'], item['status'], item['video_available']), (str(job.pk), 'failed', False))


class CancelOwnershipTests(CancelHelpers, TempMediaTestCase):
    def test_another_guests_job_looks_missing(self):
        other = Client()
        theirs = self.make_job(self.session_key(other))
        before = stored(theirs)
        self.assertApiError(self.cancel(theirs), 404, 'not_found')
        self.assertEqual(stored(theirs), before)
        self.assertEqual(self.cancel(theirs, client=other).status_code, 200)

    @override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
    def test_signed_in_owners(self):
        alice = get_user_model().objects.create_user('alice', password='unused-test-password')
        bob = get_user_model().objects.create_user('bob', password='unused-test-password')
        hers = self.make_job(user=alice)
        as_bob, as_alice = Client(), Client()
        as_bob.force_login(bob)
        as_alice.force_login(alice)
        self.assertApiError(self.cancel(hers, client=as_bob), 404, 'not_found')
        self.assertApiError(self.cancel(hers), 404, 'not_found')  # a guest session with no claim on it
        self.assertEqual(stored(hers)['status'], S.QUEUED)
        self.assertCancelled(self.cancel(hers, client=as_alice), hers)

    def test_unknown_job_and_visitor_without_session(self):
        self.assertApiError(self.cancel(uuid.uuid4()), 404, 'not_found')
        sessions = Session.objects.count()
        job = self.make_job(self.mine)
        self.assertApiError(Client().post(cancel_url(job)), 404, 'not_found')
        self.assertEqual(Session.objects.count(), sessions)  # no session created for a stranger
        self.assertEqual(stored(job)['status'], S.QUEUED)

    def test_csrf_is_required(self):
        browser = Client(enforce_csrf_checks=True)
        token = browser.get('/api/v1/csrf/').json()['csrf_token']
        self.assertEqual(browser.post(TEXT_URL, {'prompt': 'a'}, content_type='application/json',
                                      headers={'X-CSRFToken': token}).status_code, 202)
        job = GenerationJob.objects.get()
        error = self.assertApiError(browser.post(cancel_url(job)), 403, 'forbidden')
        self.assertTrue(error['message'].startswith('CSRF Failed'))
        self.assertEqual(stored(job)['status'], S.QUEUED)
        self.assertEqual(browser.post(cancel_url(job), headers={'X-CSRFToken': token}).status_code, 200)

    def test_only_post_is_allowed(self):
        job = self.make_job(self.mine)
        for method in ('get', 'put', 'patch', 'delete'):
            with self.subTest(method=method):
                self.assertApiError(getattr(self.client, method)(cancel_url(job)), 405, 'method_not_allowed')
        self.assertApiError(self.client.post(cancel_url(job).rstrip('/')), 404, 'not_found')
        self.assertEqual(stored(job)['status'], S.QUEUED)

    def test_unsafe_methods_without_a_token_get_403_before_405(self):
        # As documented: the CSRF check runs before the method check.
        browser = Client(enforce_csrf_checks=True)
        browser.cookies = self.client.cookies  # same guest session, no CSRF token
        job = self.make_job(self.mine)
        for method in ('put', 'patch', 'delete'):
            with self.subTest(method=method):
                self.assertApiError(getattr(browser, method)(cancel_url(job)), 403, 'forbidden')
        for method in ('get', 'head'):
            with self.subTest(method=method):
                self.assertEqual(getattr(browser, method)(cancel_url(job)).status_code, 405)
        self.assertEqual(stored(job)['status'], S.QUEUED)


class CancelFreesTheOwnerTests(CancelHelpers, TempMediaTestCase):
    relax_active_job_limit = False  # the real one-active-job rule
    relax_generation_limits = False  # the real rate limits

    def post_text(self):
        return self.client.post(TEXT_URL, {'prompt': 'A calm lake'}, content_type='application/json')

    def test_owner_can_submit_again_after_cancelling(self):
        first = self.post_text().json()['job_id']
        self.assertApiError(self.post_text(), 409, 'generation_in_progress')
        self.assertEqual(self.client.post(cancel_url(first)).status_code, 200)
        second = self.post_text()
        self.assertEqual(second.status_code, 202)
        self.assertEqual(GenerationJob.objects.filter(status=S.QUEUED).get().pk, uuid.UUID(second.json()['job_id']))

    def test_cancelling_does_not_use_the_generation_allowance(self):
        job = self.make_job(self.mine)
        for _ in range(10):
            self.client.post(cancel_url(job))
        self.assertEqual(self.post_text().status_code, 202)  # 5/minute allowance untouched


class CancelRaceTests(CancelHelpers, TempMediaTransactionTestCase):
    """Cancellation against a worker claiming the same job, on separate MySQL connections."""

    def hold_in_thread(self, work):
        """Run work() inside a transaction on another connection; keep it open until released."""
        ready, release, result = threading.Event(), threading.Event(), {}

        def run():
            try:
                with transaction.atomic():
                    result['value'] = work()
                    ready.set()
                    release.wait(30)
            except BaseException as exc:  # noqa: BLE001
                result['error'] = exc
                ready.set()
            finally:
                connection.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(10))
        return thread, release, result

    def test_a_claim_in_progress_wins_and_cancel_is_refused(self):
        job = self.make_job(self.mine)
        claimer, release, claimed = self.hold_in_thread(claim_next_job)
        outcome = {}
        canceller = threading.Thread(target=lambda: (outcome.setdefault('response', self.cancel(job)), connection.close()),
                                     daemon=True)
        canceller.start()
        time.sleep(0.5)
        self.assertTrue(canceller.is_alive())  # waiting for the claimed row
        release.set()
        claimer.join(10)
        canceller.join(10)
        self.assertEqual(claimed['value'].pk, job.pk)
        self.assertNotCancellable(outcome['response'], 'This video is already being generated and can no longer be cancelled.')
        self.assertEqual(stored(job)['status'], S.PROCESSING)

    def test_a_cancel_in_progress_wins_and_the_job_is_never_claimed(self):
        job = self.make_job(self.mine)
        now = timezone.now()
        cancel_update = lambda: GenerationJob.objects.filter(pk=job.pk, status=S.QUEUED).update(
            status=S.FAILED, error_code='cancelled', error_message=CANCELLED_MESSAGE, completed_at=now, updated_at=now)
        canceller, release, cancelled = self.hold_in_thread(cancel_update)
        try:
            self.assertIsNone(claim_next_job())  # the row is locked by the cancellation: skipped
        finally:
            release.set()
            canceller.join(10)
        self.assertEqual(cancelled['value'], 1)
        self.assertIsNone(claim_next_job())
        self.assertEqual(stored(job)['error_code'], 'cancelled')
