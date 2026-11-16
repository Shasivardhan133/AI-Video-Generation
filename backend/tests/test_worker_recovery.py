"""Part B: worker leases, heartbeats, safe recovery, the loop and the command.

Tests that need several threads (several database connections) use
TempMediaTransactionTestCase; they still run only on the temporary test database.
"""

import io
import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from ai_service.client import AIServiceError
from apps.generator import worker
from apps.generator.models import GenerationJob
from apps.generator.worker import (
    JobHeartbeat, claim_next_job, finish_job, process_job, recover_interrupted_jobs, run_forever,
)

from .base import MP4, PNG, TempMediaTestCase, TempMediaTransactionTestCase

S = GenerationJob.Status
FAST_LEASES = dict(WORKER_HEARTBEAT_SECONDS=0.05, WORKER_STALE_AFTER_SECONDS=0.4, WORKER_RECOVERY_INTERVAL_SECONDS=0.1)


class FakeClient:
    def __init__(self, during=None, raises=None):
        self.during, self.raises = during, raises

    def generate(self, request, output_path, on_submitted=None):
        on_submitted(f'mock-{request.job_id}')
        if self.during:
            self.during()
        if self.raises:
            raise self.raises
        Path(output_path).write_bytes(MP4)


def in_thread(target, *args):
    """Run target in a thread with its own DB connection, closed afterwards."""
    result = {}

    def run():
        try:
            result['value'] = target(*args)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            result['error'] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def wait_until(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def status_of(job):
    return GenerationJob.objects.values_list('status', flat=True).get(pk=job.pk)


class LeaseHelpers:
    def processing_job(self, heartbeat_age=0, **fields):
        job = self.make_job(status=S.PROCESSING, started_at=timezone.now(), **fields)
        GenerationJob.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(seconds=heartbeat_age))
        job.refresh_from_db()
        return job


class LeaseSettingsTests(SimpleTestCase):
    def test_defaults_are_consistent(self):
        self.assertEqual((settings.WORKER_HEARTBEAT_SECONDS, settings.WORKER_STALE_AFTER_SECONDS,
                          settings.WORKER_RECOVERY_INTERVAL_SECONDS), (30, 300, 60))
        # Many missed heartbeats are needed before a job counts as abandoned.
        self.assertGreaterEqual(settings.WORKER_STALE_AFTER_SECONDS, 5 * settings.WORKER_HEARTBEAT_SECONDS)


class RecoveryTests(LeaseHelpers, TempMediaTestCase):
    def test_active_job_of_another_worker_is_not_failed(self):
        active = self.processing_job(heartbeat_age=10)
        self.assertEqual(recover_interrupted_jobs(), 0)  # what a starting worker does
        self.assertEqual(status_of(active), S.PROCESSING)

    def test_abandoned_job_is_failed(self):
        abandoned = self.processing_job(heartbeat_age=301)
        now = timezone.now()
        self.assertEqual(recover_interrupted_jobs(now=now), 1)
        abandoned.refresh_from_db()
        self.assertEqual((abandoned.status, abandoned.error_code), (S.FAILED, 'worker_interrupted'))
        self.assertEqual(abandoned.error_message, 'Generation was interrupted. Please try again.')
        self.assertEqual(abandoned.completed_at, now)

    def test_staleness_boundary(self):
        now = timezone.now()
        just_fresh = self.processing_job()
        just_stale = self.processing_job()
        GenerationJob.objects.filter(pk=just_fresh.pk).update(updated_at=now - timedelta(seconds=299))
        GenerationJob.objects.filter(pk=just_stale.pk).update(updated_at=now - timedelta(seconds=301))
        self.assertEqual(recover_interrupted_jobs(now=now), 1)
        self.assertEqual((status_of(just_fresh), status_of(just_stale)), (S.PROCESSING, S.FAILED))

    def test_other_statuses_never_touched(self):
        old = timezone.now() - timedelta(days=1)
        jobs = [self.make_job(status=status) for status in (S.QUEUED, S.COMPLETED, S.FAILED)]
        GenerationJob.objects.update(updated_at=old)
        self.assertEqual(recover_interrupted_jobs(), 0)
        self.assertEqual([status_of(job) for job in jobs], [S.QUEUED, S.COMPLETED, S.FAILED])

    def test_recovery_is_idempotent(self):
        self.processing_job(heartbeat_age=600)
        self.assertEqual(recover_interrupted_jobs(), 1)
        self.assertEqual(recover_interrupted_jobs(), 0)


class FinishGuardTests(LeaseHelpers, TempMediaTestCase):
    def test_finish_only_while_processing(self):
        job = self.processing_job()
        self.assertTrue(finish_job(job, S.COMPLETED, output_file='outputs/x.mp4'))
        self.assertEqual(status_of(job), S.COMPLETED)
        self.assertFalse(finish_job(job, S.FAILED, error_code='late', error_message='late'))
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.COMPLETED, ''))

    def test_result_after_recovery_is_discarded(self):
        # The job's lease expires mid-generation and another worker recovers it.
        job = self.processing_job()
        later = timezone.now() + timedelta(hours=1)
        client = FakeClient(during=lambda: recover_interrupted_jobs(now=later))
        with self.assertLogs('apps.generator.worker', level='WARNING') as logs:
            process_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'worker_interrupted'))
        self.assertFalse(job.output_file)
        outputs = self.media_root / 'outputs'
        self.assertEqual([p for p in outputs.rglob('*') if p.is_file()] if outputs.exists() else [], [])
        self.assertTrue(any('lease lost' in line for line in logs.output))

    def test_late_error_does_not_overwrite_recovery(self):
        job = self.processing_job()
        later = timezone.now() + timedelta(hours=1)
        client = FakeClient(during=lambda: recover_interrupted_jobs(now=later),
                            raises=AIServiceError('service_timeout', 'Too slow.'))
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            process_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.error_code, 'worker_interrupted')

    # 3.5.1: a job deleted while it is processing.
    def output_files(self):
        outputs = self.media_root / 'outputs'
        return sorted(p.relative_to(self.media_root).as_posix() for p in outputs.rglob('*') if p.is_file()) \
            if outputs.exists() else []

    def test_finish_on_a_deleted_job_reports_not_recorded(self):
        job = self.processing_job()
        GenerationJob.objects.filter(pk=job.pk).delete()
        for status, fields in ((S.COMPLETED, {'output_file': 'outputs/x.mp4'}),
                               (S.FAILED, {'error_code': 'x', 'error_message': 'y'})):
            with self.subTest(status=status), self.assertLogs('apps.generator.worker', level='WARNING') as logs:
                self.assertIs(finish_job(job, status, **fields), False)
            self.assertTrue(any('no longer exists' in line and str(job.id) in line for line in logs.output))
            self.assertFalse(any('lease lost' in line for line in logs.output))
        self.assertFalse(GenerationJob.objects.filter(pk=job.pk).exists())  # never recreated

    def test_job_deleted_during_generation_discards_only_its_new_output(self):
        other = self.make_completed_job()
        job = self.processing_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        job.input_file.save('in.png', ContentFile(PNG), save=True)
        before = self.output_files()
        delete = lambda: GenerationJob.objects.filter(pk=job.pk).delete()
        with self.assertLogs('apps.generator.worker', level='INFO') as logs:
            process_job(job, client=FakeClient(during=delete))  # must not raise
        self.assertTrue(any('no longer exists' in line for line in logs.output))
        self.assertTrue(any('discarded output' in line and 'deleted' in line for line in logs.output))
        self.assertFalse(any('could not record its result' in line for line in logs.output))
        self.assertEqual(self.output_files(), before)  # the new file is gone, nothing else
        self.assertTrue((self.media_root / other.output_file.name).exists())
        self.assertTrue((self.media_root / job.input_file.name).exists())  # not the worker's to delete
        self.assertFalse(GenerationJob.objects.filter(pk=job.pk).exists())
        self.assertEqual(status_of(other), S.COMPLETED)

    def test_job_deleted_before_an_ai_error_is_recorded(self):
        job = self.processing_job()
        client = FakeClient(during=lambda: GenerationJob.objects.filter(pk=job.pk).delete(),
                            raises=AIServiceError('service_timeout', 'Too slow.'))
        with self.assertLogs('apps.generator.worker', level='WARNING') as logs:
            process_job(job, client=client)  # must not raise
        self.assertTrue(any('no longer exists' in line for line in logs.output))
        self.assertFalse(GenerationJob.objects.filter(pk=job.pk).exists())
        self.assertEqual(self.output_files(), [])

    def test_invalid_job_deleted_before_it_is_failed(self):
        job = self.processing_job(settings={'model_profile': 'retired-profile'})
        GenerationJob.objects.filter(pk=job.pk).delete()
        with self.assertLogs('apps.generator.worker', level='WARNING') as logs:
            process_job(job, client=FakeClient())  # must not raise
        self.assertTrue(any('no longer exists' in line for line in logs.output))

    def test_remote_id_not_written_to_recovered_job(self):
        job = self.processing_job()
        GenerationJob.objects.filter(pk=job.pk).update(status=S.FAILED)
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            process_job(job, client=FakeClient())
        job.refresh_from_db()
        self.assertEqual((job.status, job.remote_job_id), (S.FAILED, ''))


@override_settings(**FAST_LEASES)
class HeartbeatTests(LeaseHelpers, TempMediaTransactionTestCase):
    def test_heartbeat_keeps_an_active_job_alive(self):
        job = self.processing_job()
        with JobHeartbeat(job.pk, 0.05):
            time.sleep(1.0)  # far longer than the 0.4 s stale limit
            self.assertEqual(recover_interrupted_jobs(), 0)
        self.assertEqual(status_of(job), S.PROCESSING)
        # Without heartbeats the job goes stale and is recovered.
        time.sleep(0.6)
        self.assertEqual(recover_interrupted_jobs(), 1)

    def test_long_generation_survives_other_workers_recovering(self):
        job = self.processing_job()
        recovered = []

        def slow():
            for _ in range(10):
                time.sleep(0.1)
                thread, result = in_thread(recover_interrupted_jobs)  # another worker starting up
                thread.join()
                recovered.append(result['value'])

        process_job(job, client=FakeClient(during=slow))
        self.assertEqual(sum(recovered), 0)
        self.assertEqual(status_of(job), S.COMPLETED)

    def test_heartbeat_stops_when_job_is_no_longer_processing(self):
        job = self.processing_job()
        with JobHeartbeat(job.pk, 0.05) as heartbeat:
            GenerationJob.objects.filter(pk=job.pk).update(status=S.FAILED)
            self.assertTrue(wait_until(lambda: heartbeat.lost, timeout=3))
            self.assertTrue(wait_until(lambda: not heartbeat._thread.is_alive(), timeout=3))

    def test_heartbeat_thread_ends_on_exit(self):
        job = self.processing_job()
        with JobHeartbeat(job.pk, 30) as heartbeat:
            pass
        self.assertFalse(heartbeat._thread.is_alive())

    def test_heartbeat_survives_a_database_error(self):
        job = self.processing_job(heartbeat_age=0.3)
        real_filter = GenerationJob.objects.filter
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('database unavailable')
            return real_filter(*args, **kwargs)

        before = GenerationJob.objects.get(pk=job.pk).updated_at
        with self.assertLogs('apps.generator.worker', level='ERROR') as logs, \
                mock.patch.object(GenerationJob.objects, 'filter', side_effect=flaky):
            with JobHeartbeat(job.pk, 0.05):
                self.assertTrue(wait_until(lambda: len(calls) >= 3, timeout=3))
        self.assertIn('Heartbeat for job', logs.output[0])
        self.assertGreater(GenerationJob.objects.get(pk=job.pk).updated_at, before)


class ConcurrencyTests(LeaseHelpers, TempMediaTransactionTestCase):
    def test_two_workers_never_claim_the_same_job(self):
        jobs = {str(self.make_job(minutes_ago=i).pk) for i in range(20)}
        barrier = threading.Barrier(2)

        def drain():
            barrier.wait()
            claimed = []
            while (job := claim_next_job()) is not None:
                claimed.append(str(job.pk))
            return claimed

        threads = [in_thread(drain) for _ in range(2)]
        for thread, _ in threads:
            thread.join(30)
        first, second = (result['value'] for _, result in threads)
        self.assertEqual(set(first) & set(second), set())
        self.assertEqual(set(first) | set(second), jobs)
        self.assertEqual(len(first) + len(second), 20)
        self.assertEqual(GenerationJob.objects.filter(status=S.PROCESSING).count(), 20)

    def test_two_workers_recover_each_abandoned_job_once(self):
        for _ in range(10):
            self.processing_job(heartbeat_age=600)
        barrier = threading.Barrier(2)

        def recover():
            barrier.wait()
            return recover_interrupted_jobs()

        threads = [in_thread(recover) for _ in range(2)]
        for thread, _ in threads:
            thread.join(30)
        self.assertEqual(sum(result['value'] for _, result in threads), 10)
        self.assertEqual(GenerationJob.objects.filter(status=S.FAILED, error_code='worker_interrupted').count(), 10)


@override_settings(**FAST_LEASES)
class WorkerLoopTests(LeaseHelpers, TempMediaTransactionTestCase):
    def start_loop(self):
        stop = threading.Event()
        thread, result = in_thread(run_forever, 0.05, stop)
        self.addCleanup(lambda: (stop.set(), thread.join(10)))
        return stop, thread, result

    def test_processes_queued_jobs_and_stops_cleanly(self):
        jobs = [self.make_job(minutes_ago=i) for i in range(3)]
        with mock.patch.object(worker, 'get_client', return_value=FakeClient()):
            stop, thread, result = self.start_loop()
            self.assertTrue(wait_until(lambda: all(status_of(j) == S.COMPLETED for j in jobs)))
            stop.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('error', result)

    def test_startup_and_periodic_recovery_only_fail_abandoned_jobs(self):
        abandoned_before = self.processing_job(heartbeat_age=600)
        active = self.processing_job()
        with JobHeartbeat(active.pk, 0.05), mock.patch.object(worker, 'get_client', return_value=FakeClient()):
            self.start_loop()
            self.assertTrue(wait_until(lambda: status_of(abandoned_before) == S.FAILED))
            abandoned_later = self.processing_job(heartbeat_age=600)  # a worker crashes while this one runs
            self.assertTrue(wait_until(lambda: status_of(abandoned_later) == S.FAILED))
            time.sleep(0.5)
            self.assertEqual(status_of(active), S.PROCESSING)

    def test_claim_errors_are_logged_and_the_loop_continues(self):
        job = self.make_job()
        real_claim = worker.claim_next_job
        calls = []

        def flaky_claim():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('database unavailable')
            return real_claim()

        with self.assertLogs('apps.generator.worker', level='ERROR') as logs, \
                mock.patch.object(worker, 'claim_next_job', side_effect=flaky_claim), \
                mock.patch.object(worker, 'get_client', return_value=FakeClient()):
            self.start_loop()
            self.assertTrue(wait_until(lambda: status_of(job) == S.COMPLETED))
        self.assertTrue(any('Could not claim a job' in line for line in logs.output))

    def test_processing_crash_keeps_the_worker_alive(self):
        crashing, normal = self.make_job(minutes_ago=2), self.make_job(minutes_ago=1)
        real_process = worker.process_job

        def crash_first(job, client=None):
            if job.pk == crashing.pk:
                raise RuntimeError('worker bug')
            return real_process(job, client)

        with self.assertLogs('apps.generator.worker', level='ERROR'), \
                mock.patch.object(worker, 'process_job', side_effect=crash_first), \
                mock.patch.object(worker, 'get_client', return_value=FakeClient()):
            self.start_loop()
            self.assertTrue(wait_until(lambda: status_of(normal) == S.COMPLETED))
            # The crashed job keeps its lease until it goes stale, then is recovered.
            self.assertTrue(wait_until(lambda: status_of(crashing) == S.FAILED))
        self.assertEqual(GenerationJob.objects.get(pk=crashing.pk).error_code, 'worker_interrupted')

    def test_stop_during_a_job_fails_it_and_stops(self):
        job = self.make_job()
        with mock.patch.object(worker, 'process_job', side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            run_forever(poll_seconds=0.05)
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'worker_interrupted'))


class ManagementCommandTests(TempMediaTransactionTestCase):
    def test_command_processes_jobs_and_stops_on_ctrl_c(self):
        job = self.make_job()
        real_claim = worker.claim_next_job
        calls = []

        def claim_then_stop():
            calls.append(1)
            if len(calls) > 1:
                raise KeyboardInterrupt
            return real_claim()

        out = io.StringIO()
        with mock.patch.object(worker, 'claim_next_job', side_effect=claim_then_stop), \
                mock.patch.object(worker, 'get_client', return_value=FakeClient()):
            call_command('run_generation_worker', '--poll-seconds', '0.05', stdout=out)
        self.assertIn('Worker stopped.', out.getvalue())
        self.assertEqual(status_of(job), S.COMPLETED)

    def test_poll_seconds_option(self):
        with mock.patch('apps.generator.management.commands.run_generation_worker.run_forever',
                        side_effect=KeyboardInterrupt) as loop:
            call_command('run_generation_worker', stdout=io.StringIO())
            call_command('run_generation_worker', '--poll-seconds', '0.5', stdout=io.StringIO())
        self.assertEqual([c.kwargs['poll_seconds'] for c in loop.call_args_list], [2.0, 0.5])

    def test_poll_seconds_must_be_a_positive_number(self):
        with mock.patch('apps.generator.management.commands.run_generation_worker.run_forever') as loop:
            for value in ('0', '-1', 'nan', 'inf'):
                with self.subTest(value=value):
                    with self.assertRaisesMessage(CommandError, '--poll-seconds must be a number greater than 0'):
                        call_command('run_generation_worker', '--poll-seconds', value, stdout=io.StringIO())
        loop.assert_not_called()


class StartupIntegrationTests(SimpleTestCase):
    def ready(self, argv, run_main, enabled=True):
        config = apps.get_app_config('generator')
        with mock.patch('apps.generator.worker.start_in_background') as start, \
                mock.patch('sys.argv', argv), \
                mock.patch.dict('os.environ', {'RUN_MAIN': run_main} if run_main else {}, clear=False), \
                override_settings(RUN_WORKER_IN_PROCESS=enabled):
            if not run_main:
                import os
                os.environ.pop('RUN_MAIN', None)
            config.ready()
        return start.called

    def test_starts_only_in_the_runserver_child_process(self):
        self.assertTrue(self.ready(['manage.py', 'runserver'], 'true'))
        self.assertFalse(self.ready(['manage.py', 'runserver'], None))  # autoreloader parent
        self.assertFalse(self.ready(['manage.py', 'run_generation_worker'], 'true'))
        self.assertFalse(self.ready(['manage.py', 'test'], 'true'))
        self.assertFalse(self.ready(['manage.py', 'runserver'], 'true', enabled=False))

    def test_background_thread(self):
        with mock.patch.object(worker, 'run_forever') as loop:
            thread = worker.start_in_background()
            thread.join(5)
        self.assertEqual((thread.name, thread.daemon), ('generation-worker', True))
        loop.assert_called_once_with()
