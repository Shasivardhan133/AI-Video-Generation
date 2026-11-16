"""3.5.4 (A): queued jobs that wait longer than GENERATION_QUEUE_TIMEOUT_SECONDS fail
with `queue_timeout`, from the worker's recovery cycle or the expire_stale_jobs command.

Concurrency tests use several real MySQL connections on the temporary test database.
"""

import io
import logging
import os
import threading
import time
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection, transaction
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.generator.models import GenerationJob
from apps.generator.worker import (
    QUEUE_TIMEOUT_CODE, QUEUE_TIMEOUT_MESSAGE, claim_next_job, expire_queued_jobs, recover_stale_jobs, run_forever,
)
from config.settings import env_positive_int

from .base import PNG, TempMediaTestCase, TempMediaTransactionTestCase

S = GenerationJob.Status
TIMEOUT = 1800


class QuietWorkerLogs:
    """Expiry logs a warning each time; keep it off the test console (assertLogs still sees it)."""

    def setUp(self):
        super().setUp()
        quiet = mock.patch.object(logging.getLogger('apps'), 'handlers', [logging.NullHandler()])
        quiet.start()
        self.addCleanup(quiet.stop)


class QueueTimeoutSettingTests(SimpleTestCase):
    def test_default_is_thirty_minutes(self):
        # Unless a developer's .env sets it, in which case that value must be in effect.
        configured = os.environ.get('GENERATION_QUEUE_TIMEOUT_SECONDS', '').strip()
        self.assertEqual(settings.GENERATION_QUEUE_TIMEOUT_SECONDS, int(configured) if configured else TIMEOUT)
        self.assertEqual(TIMEOUT, 30 * 60)

    def test_environment_values(self):
        name = 'GEETHA_TEST_POSITIVE_INT'
        cases = {'': 1800, '  ': 1800, '60': 60, ' 900 ': 900}
        for raw, expected in cases.items():
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {name: raw}):
                self.assertEqual(env_positive_int(name, 1800), expected)
        for raw in ('0', '-5', '1.5', 'abc', '30m', '１２'):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {name: raw}), \
                    self.assertRaisesMessage(ImproperlyConfigured, name):
                env_positive_int(name, 1800)


class QueueTimeoutTests(QuietWorkerLogs, TempMediaTestCase):
    def queued(self, seconds_ago, **fields):
        return self.make_job(created_at=timezone.now() - timedelta(seconds=seconds_ago), **fields)

    def test_boundary(self):
        now = timezone.now()
        at_limit = self.make_job(created_at=now - timedelta(seconds=TIMEOUT))
        just_past = self.make_job(created_at=now - timedelta(seconds=TIMEOUT, milliseconds=1))
        young = self.make_job(created_at=now - timedelta(seconds=TIMEOUT - 1))
        self.assertEqual(expire_queued_jobs(now=now), 1)
        statuses = {job.pk: GenerationJob.objects.get(pk=job.pk).status for job in (at_limit, just_past, young)}
        self.assertEqual(statuses, {at_limit.pk: S.QUEUED, just_past.pk: S.FAILED, young.pk: S.QUEUED})

    def test_expired_job_fields(self):
        job = self.queued(TIMEOUT + 60, mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        job.input_file.save('in.png', ContentFile(PNG), save=True)
        now = timezone.now()
        self.assertEqual(expire_queued_jobs(now=now), 1)
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code, job.error_message), (S.FAILED, QUEUE_TIMEOUT_CODE, QUEUE_TIMEOUT_MESSAGE))
        self.assertEqual(job.completed_at, now)
        self.assertIsNone(job.started_at)  # it never started
        self.assertTrue((self.media_root / job.input_file.name).exists())  # the image is kept

    def test_only_queued_jobs_expire(self):
        old = timezone.now() - timedelta(seconds=TIMEOUT * 3)
        others = [self.make_job(created_at=old, status=status, started_at=old)
                  for status in (S.PROCESSING, S.COMPLETED, S.FAILED)]
        before = {job.pk: GenerationJob.objects.filter(pk=job.pk).values().get() for job in others}
        self.assertEqual(expire_queued_jobs(), 0)
        for job in others:
            self.assertEqual(GenerationJob.objects.filter(pk=job.pk).values().get(), before[job.pk])

    def test_repeated_runs_expire_each_job_once(self):
        job = self.queued(TIMEOUT + 5)
        self.assertEqual(expire_queued_jobs(), 1)
        first = GenerationJob.objects.filter(pk=job.pk).values().get()
        for _ in range(3):
            self.assertEqual(expire_queued_jobs(now=timezone.now() + timedelta(hours=1)), 0)
        self.assertEqual(GenerationJob.objects.filter(pk=job.pk).values().get(), first)

    @override_settings(GENERATION_QUEUE_TIMEOUT_SECONDS=60)
    def test_configured_timeout_is_used(self):
        old, young = self.queued(61), self.queued(30)
        self.assertEqual(expire_queued_jobs(), 1)
        self.assertEqual((GenerationJob.objects.get(pk=old.pk).status, GenerationJob.objects.get(pk=young.pk).status),
                         (S.FAILED, S.QUEUED))

    def test_a_worker_never_starts_an_expired_job(self):
        expired = self.queued(TIMEOUT + 10)  # older, so first in line, but past the timeout
        fresh = self.queued(5)
        claimed = claim_next_job()
        self.assertEqual(claimed.pk, fresh.pk)
        self.assertIsNone(claim_next_job())
        self.assertEqual(GenerationJob.objects.get(pk=expired.pk).status, S.QUEUED)  # left for expiry
        self.assertEqual(expire_queued_jobs(), 1)

    def test_recovery_cycle_does_both(self):
        expired = self.queued(TIMEOUT + 10)
        abandoned = self.make_job(status=S.PROCESSING, started_at=timezone.now())
        GenerationJob.objects.filter(pk=abandoned.pk).update(updated_at=timezone.now() - timedelta(seconds=301))
        self.assertEqual(recover_stale_jobs(), (1, 1))
        self.assertEqual(GenerationJob.objects.get(pk=expired.pk).error_code, QUEUE_TIMEOUT_CODE)
        self.assertEqual(GenerationJob.objects.get(pk=abandoned.pk).error_code, 'worker_interrupted')
        self.assertEqual(recover_stale_jobs(), (0, 0))

    def test_worker_expires_old_jobs_when_it_starts(self):
        expired = self.queued(TIMEOUT + 10)
        stop = threading.Event()
        stop.set()  # run only the startup recovery
        with self.assertLogs('apps.generator.worker', level='WARNING') as logs:
            run_forever(stop_event=stop)
        self.assertEqual(GenerationJob.objects.get(pk=expired.pk).error_code, QUEUE_TIMEOUT_CODE)
        self.assertTrue(any('waited too long in the queue' in line for line in logs.output))

    def test_api_reports_the_timeout(self):
        response = self.client.post('/api/v1/generations/text-to-video/', {'prompt': 'A calm lake'},
                                    content_type='application/json')
        job_id = response.json()['job_id']
        GenerationJob.objects.filter(pk=job_id).update(created_at=timezone.now() - timedelta(seconds=TIMEOUT + 1))
        expire_queued_jobs()
        body = self.client.get(f'/api/v1/generations/{job_id}/').json()
        self.assertEqual(body['status'], 'failed')
        self.assertEqual(body['error']['code'], QUEUE_TIMEOUT_CODE)
        self.assertEqual(body['error']['message'], QUEUE_TIMEOUT_MESSAGE)


class ExpiredJobFreesTheOwnerTests(QuietWorkerLogs, TempMediaTestCase):
    relax_active_job_limit = False

    def test_owner_can_submit_again_after_expiry(self):
        post = lambda: self.client.post('/api/v1/generations/text-to-video/', {'prompt': 'A calm lake'},
                                        content_type='application/json')
        job_id = post().json()['job_id']
        self.assertApiError(post(), 409, 'generation_in_progress')
        GenerationJob.objects.filter(pk=job_id).update(created_at=timezone.now() - timedelta(seconds=TIMEOUT + 1))
        self.assertEqual(expire_queued_jobs(), 1)
        self.assertEqual(post().status_code, 202)


class ExpireCommandTests(TempMediaTestCase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command('expire_stale_jobs', *args, stdout=out)
        return out.getvalue().strip()

    def test_reports_and_expires(self):
        expired = self.make_job(created_at=timezone.now() - timedelta(seconds=TIMEOUT + 10))
        young = self.make_job()
        abandoned = self.make_job(status=S.PROCESSING, started_at=timezone.now())
        GenerationJob.objects.filter(pk=abandoned.pk).update(updated_at=timezone.now() - timedelta(seconds=301))
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            output = self.run_command()
        self.assertEqual(output, 'Expired 1 queued job(s) (queue_timeout). Recovered 1 interrupted job(s) (worker_interrupted).')
        self.assertEqual(GenerationJob.objects.get(pk=expired.pk).error_code, QUEUE_TIMEOUT_CODE)
        self.assertEqual(GenerationJob.objects.get(pk=young.pk).status, S.QUEUED)
        self.assertEqual(GenerationJob.objects.get(pk=abandoned.pk).error_code, 'worker_interrupted')

    def test_repeated_runs_report_zero(self):
        self.make_job(created_at=timezone.now() - timedelta(seconds=TIMEOUT + 10))
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            self.run_command()
        self.assertEqual(self.run_command(), 'Expired 0 queued job(s) (queue_timeout). Recovered 0 interrupted job(s) (worker_interrupted).')

    def test_dry_run_changes_nothing(self):
        job = self.make_job(created_at=timezone.now() - timedelta(seconds=TIMEOUT + 10))
        before = GenerationJob.objects.filter(pk=job.pk).values().get()
        self.assertEqual(self.run_command('--dry-run'),
                         f'1 queued job(s) have waited longer than {TIMEOUT} s. Dry run: nothing was changed.')
        self.assertEqual(GenerationJob.objects.filter(pk=job.pk).values().get(), before)

    def test_reuses_the_worker_recovery_cycle(self):
        with mock.patch('apps.generator.management.commands.expire_stale_jobs.recover_stale_jobs',
                        return_value=(2, 3)) as cycle:
            self.assertEqual(self.run_command(), 'Expired 3 queued job(s) (queue_timeout). Recovered 2 interrupted job(s) (worker_interrupted).')
        cycle.assert_called_once_with()


class ConcurrentExpiryTests(QuietWorkerLogs, TempMediaTransactionTestCase):
    """Expiry against a worker claiming the same job, each on its own MySQL connection."""

    def in_thread(self, target):
        result = {}

        def run():
            try:
                result['value'] = target()
            except BaseException as exc:  # noqa: BLE001 - re-raised in the test
                result['error'] = exc
            finally:
                connection.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    def test_expiry_waits_for_a_claim_in_progress_and_then_skips_the_job(self):
        job = self.make_job()
        claimed, release = threading.Event(), threading.Event()

        def claim_and_hold():
            with transaction.atomic():  # the claim's row lock is held until release
                outcome = claim_next_job()
                claimed.set()
                release.wait(30)
            return outcome

        claimer, claim_result = self.in_thread(claim_and_hold)
        self.assertTrue(claimed.wait(10))
        expirer, expire_result = self.in_thread(lambda: expire_queued_jobs(now=timezone.now() + timedelta(days=1)))
        time.sleep(0.5)
        self.assertTrue(expirer.is_alive())  # blocked on the claimed row
        release.set()
        claimer.join(10)
        expirer.join(10)
        self.assertNotIn('error', claim_result)
        self.assertEqual(claim_result['value'].pk, job.pk)
        self.assertEqual(expire_result, {'value': 0})  # the job had started: never expired
        self.assertEqual(GenerationJob.objects.get(pk=job.pk).status, S.PROCESSING)

    def test_a_job_being_expired_is_skipped_by_a_claiming_worker(self):
        job = self.make_job(created_at=timezone.now() - timedelta(seconds=10))
        expiring, release = threading.Event(), threading.Event()

        def expire_and_hold():
            with transaction.atomic():
                count = expire_queued_jobs(now=timezone.now() + timedelta(days=1))
                expiring.set()
                release.wait(30)
            return count

        expirer, expire_result = self.in_thread(expire_and_hold)
        self.assertTrue(expiring.wait(10))
        try:
            self.assertIsNone(claim_next_job())  # locked row skipped; never started
        finally:
            release.set()
            expirer.join(10)
        self.assertEqual(expire_result, {'value': 1})
        self.assertEqual(GenerationJob.objects.get(pk=job.pk).error_code, QUEUE_TIMEOUT_CODE)
        self.assertIsNone(claim_next_job())

    def test_concurrent_expiry_runs_expire_each_job_once(self):
        for _ in range(3):
            self.make_job(created_at=timezone.now() - timedelta(seconds=TIMEOUT + 10))
        barrier = threading.Barrier(4)
        runs = [self.in_thread(lambda: (barrier.wait(10), expire_queued_jobs())[1]) for _ in range(4)]
        for thread, _ in runs:
            thread.join(20)
        self.assertEqual(sum(result['value'] for _, result in runs), 3)
        self.assertEqual(GenerationJob.objects.filter(error_code=QUEUE_TIMEOUT_CODE).count(), 3)
