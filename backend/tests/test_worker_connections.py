"""Worker behaviour when real MySQL connections break, and row-lock ordering.

These tests kill real database sessions, but only sessions they opened
themselves on the temporary test database (checked before every KILL). Work
that must keep one connection across several steps runs on a DedicatedThread,
because Django connections belong to the thread that opened them.
"""

import queue
import threading
from datetime import datetime, timedelta
from unittest import mock

from django.core.files.base import ContentFile
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from apps.generator import worker
from apps.generator.models import GenerationJob
from apps.generator.worker import JobHeartbeat, finish_job, process_job, recover_interrupted_jobs

from .base import MP4, PNG, TempMediaTransactionTestCase
from .test_worker_recovery import FakeClient, LeaseHelpers, status_of, wait_until

S = GenerationJob.Status
STALE = timedelta(seconds=301)  # just past the default 300 s stale limit


class DedicatedThread:
    """Runs calls one at a time on a single thread, so they share its connection."""

    def __init__(self):
        self._calls = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        try:
            while (item := self._calls.get()) is not None:
                func, outcome = item
                try:
                    outcome['value'] = func()
                except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
                    outcome['error'] = exc
                outcome['done'].set()
        finally:
            worker.discard_connection()

    def submit(self, func):
        outcome = {'done': threading.Event()}
        self._calls.put((func, outcome))
        return outcome

    @staticmethod
    def result(outcome, timeout=30):
        if not outcome['done'].wait(timeout):  # not `assert`: under -O the wait itself would vanish
            raise AssertionError('call did not finish')
        if 'error' in outcome:
            raise outcome['error']
        return outcome['value']

    def call(self, func, timeout=30):
        return self.result(self.submit(func), timeout)

    def stop(self):
        self._calls.put(None)
        self._thread.join(10)


def connection_id():
    """MySQL session id of the calling thread's connection (opening it if needed)."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT CONNECTION_ID()')
        return cursor.fetchone()[0]


def lock_waiting(session_id):
    """True while that session's transaction waits for a row lock."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM information_schema.INNODB_TRX "
                       "WHERE trx_mysql_thread_id = %s AND trx_state = 'LOCK WAIT'", [session_id])
        return cursor.fetchone()[0] == 1


class ConnectionTestCase(LeaseHelpers, TempMediaTransactionTestCase):
    def setUp(self):
        super().setUp()
        self.test_db = connection.settings_dict['NAME']
        if not self.test_db.startswith('test_'):
            raise RuntimeError('refusing to run outside the test database')
        self.killer = self.thread()

    def thread(self):
        runner = DedicatedThread()
        self.addCleanup(runner.stop)
        return runner

    def kill(self, session_id):
        """Kill one of this test's own sessions, from a separate connection."""
        def run():
            with connection.cursor() as cursor:
                cursor.execute('SELECT DB FROM information_schema.PROCESSLIST WHERE ID = %s', [session_id])
                row = cursor.fetchone()
                if not (row and row[0] == self.test_db):
                    raise RuntimeError('refusing to kill a session outside the test database')
                cursor.execute('KILL %s' % int(session_id))
        self.killer.call(run)

        def gone():
            with connection.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID = %s', [session_id])
                return cursor.fetchone()[0] == 0
        self.assertTrue(wait_until(lambda: self.killer.call(gone)))

    def kill_own_connection(self):
        """Kill the calling thread's connection. Django still holds it, as after a server restart."""
        self.kill(connection_id())

    def lease_of(self, job):
        return GenerationJob.objects.values_list('updated_at', flat=True).get(pk=job.pk)

    def age_lease(self, job, seconds=200):
        GenerationJob.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(seconds=seconds))
        return self.lease_of(job)


class HeartbeatReconnectTests(ConnectionTestCase):
    def test_killed_connection_is_replaced_and_lease_renewed(self):
        job = self.processing_job()
        heartbeat, runner = JobHeartbeat(job.pk, 30), self.thread()
        self.assertEqual(runner.call(heartbeat.beat), JobHeartbeat.ALIVE)
        first = runner.call(connection_id)
        self.kill(first)
        aged = self.age_lease(job)

        with self.assertLogs('apps.generator.worker', level='INFO') as logs:
            self.assertEqual(runner.call(heartbeat.beat), JobHeartbeat.ALIVE)
        self.assertTrue(any('connection was lost' in line for line in logs.output))
        self.assertNotEqual(runner.call(connection_id), first)
        self.assertGreater(self.lease_of(job), aged)
        self.assertLess(timezone.now() - self.lease_of(job), timedelta(seconds=30))

    def test_failure_during_the_update_reconnects_on_the_next_heartbeat(self):
        # The connection dies after the ping, so the UPDATE itself fails for real.
        job = self.processing_job()
        heartbeat, runner = JobHeartbeat(job.pk, 30), self.thread()
        runner.call(heartbeat.beat)
        self.kill(runner.call(connection_id))
        aged = self.age_lease(job)
        with mock.patch.object(worker, 'drop_broken_connection'), \
                self.assertLogs('apps.generator.worker', level='ERROR') as logs:
            self.assertEqual(runner.call(heartbeat.beat), JobHeartbeat.FAILED)
        self.assertIn('Heartbeat for job', logs.output[0])
        self.assertEqual((heartbeat.failures, self.lease_of(job)), (1, aged))

        with self.assertLogs('apps.generator.worker', level='INFO') as logs:
            self.assertEqual(runner.call(heartbeat.beat), JobHeartbeat.ALIVE)
        self.assertTrue(any('recovered after 1 failed attempt' in line for line in logs.output))
        self.assertEqual(heartbeat.failures, 0)
        self.assertGreater(self.lease_of(job), aged)

    def test_running_heartbeat_survives_a_killed_connection(self):
        job = self.processing_job()
        sessions, real_renew = [], JobHeartbeat.renew

        def recording_renew(heartbeat):
            renewed = real_renew(heartbeat)
            sessions.append(connection.connection.thread_id())
            return renewed

        with mock.patch.object(JobHeartbeat, 'renew', recording_renew), \
                self.assertLogs('apps.generator.worker', level='INFO'), JobHeartbeat(job.pk, 0.05) as heartbeat:
            self.assertTrue(wait_until(lambda: sessions))
            self.kill(sessions[-1])
            aged = self.age_lease(job)
            self.assertTrue(wait_until(lambda: self.lease_of(job) > aged))
            self.assertGreater(len(set(sessions)), 1)  # it reconnected
        self.assertEqual((status_of(job), heartbeat.lost), (S.PROCESSING, False))


class LongOutageTests(ConnectionTestCase):
    def test_failures_beyond_the_stale_limit_never_revive_a_recovered_job(self):
        job = self.processing_job()
        heartbeat, runner = JobHeartbeat(job.pk, 30), self.thread()
        self.assertEqual(runner.call(heartbeat.beat), JobHeartbeat.ALIVE)

        # Every heartbeat fails for real (its session is killed each time) ...
        with mock.patch.object(worker, 'drop_broken_connection'), \
                self.assertLogs('apps.generator.worker', level='WARNING') as logs:
            for attempt in range(3):
                self.kill(runner.call(connection_id))
                if attempt == 2:
                    heartbeat._last_renewed -= 301  # ... for longer than the stale limit
                self.assertEqual(runner.call(heartbeat.beat), JobHeartbeat.FAILED)
        self.assertEqual(heartbeat.failures, 3)
        self.assertEqual(sum('lease has probably expired' in line for line in logs.output), 1)

        # ... so another worker recovers the job.
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            self.assertEqual(recover_interrupted_jobs(now=timezone.now() + STALE), 1)
        recovered_lease = self.lease_of(job)

        # The database is back: the heartbeat must notice, not revive the job.
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            self.assertEqual(runner.call(heartbeat.beat), JobHeartbeat.LOST)
        job.refresh_from_db()
        self.assertTrue(heartbeat.lost)
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'worker_interrupted'))
        self.assertEqual(job.updated_at, recovered_lease)
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            self.assertFalse(finish_job(job, S.COMPLETED, output_file='outputs/late.mp4'))
        self.assertEqual(status_of(job), S.FAILED)


class RowLockOrderTests(ConnectionTestCase):
    """Two connections update the same row; the one holding the row lock first wins."""

    def setUp(self):
        super().setUp()
        self.locked, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)  # never leave a transaction open

    def hold(self, func):
        """Run func in a transaction and keep its row locks until release."""
        def run():
            with transaction.atomic():
                value = func()
                self.locked.set()
                self.release.wait(30)
            return value
        return run

    def assert_waits_for_lock(self, session_id):
        self.assertTrue(wait_until(lambda: self.killer.call(lambda: lock_waiting(session_id))),
                        'second update never waited for the row lock')

    def test_heartbeat_first_recovery_skips_the_renewed_job(self):
        job = self.processing_job(heartbeat_age=600)  # stale as committed so far
        heartbeat = JobHeartbeat(job.pk, 30)
        holder, other = self.thread(), self.thread()
        renewing = holder.submit(self.hold(heartbeat.renew))
        self.assertTrue(self.locked.wait(10))

        recovering_session = other.call(connection_id)
        recovering = other.submit(recover_interrupted_jobs)
        self.assert_waits_for_lock(recovering_session)
        self.release.set()

        self.assertEqual(DedicatedThread.result(renewing), 1)
        self.assertEqual(DedicatedThread.result(recovering), 0)
        self.assertEqual(status_of(job), S.PROCESSING)
        self.assertLess(timezone.now() - self.lease_of(job), timedelta(seconds=30))

    def test_recovery_first_heartbeat_does_not_revive_the_job(self):
        job = self.processing_job(heartbeat_age=600)
        heartbeat = JobHeartbeat(job.pk, 30)
        holder, other = self.thread(), self.thread()
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            recovering = holder.submit(self.hold(recover_interrupted_jobs))
            self.assertTrue(self.locked.wait(10))
            heartbeat_session = other.call(connection_id)
            beating = other.submit(heartbeat.beat)
            self.assert_waits_for_lock(heartbeat_session)
            recovered_lease = timezone.now()
            self.release.set()
            self.assertEqual(DedicatedThread.result(recovering), 1)
            self.assertEqual(DedicatedThread.result(beating), JobHeartbeat.LOST)
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'worker_interrupted'))
        self.assertLess(job.updated_at, recovered_lease)  # recovery's timestamp, not a heartbeat's


class ResultSavingTests(ConnectionTestCase):
    def output_files(self):
        outputs = self.media_root / 'outputs'
        return sorted(p.relative_to(self.media_root).as_posix() for p in outputs.rglob('*') if p.is_file()) \
            if outputs.exists() else []

    def test_stale_connection_is_replaced_before_recording_the_result(self):
        job = self.processing_job()
        with self.assertLogs('apps.generator.worker', level='INFO') as logs:
            process_job(job, client=FakeClient(during=self.kill_own_connection))
        self.assertTrue(any('connection was lost' in line for line in logs.output))
        job.refresh_from_db()
        self.assertEqual(job.status, S.COMPLETED)
        self.assertEqual(self.output_files(), [job.output_file.name])
        self.assertEqual((self.media_root / job.output_file.name).read_bytes(), MP4)

    def test_write_that_failed_is_rechecked_and_retried_once(self):
        # The connection dies after the ping: the UPDATE fails for real and did not commit.
        job = self.processing_job()
        writes, real_write = [], worker.write_if_processing

        def counting_write(*args):
            writes.append(1)
            return real_write(*args)

        with mock.patch.object(worker, 'drop_broken_connection'), \
                mock.patch.object(worker, 'write_if_processing', counting_write), \
                self.assertLogs('apps.generator.worker', level='WARNING') as logs:
            process_job(job, client=FakeClient(during=self.kill_own_connection))
        self.assertTrue(any('reconnecting to check the job' in line for line in logs.output))
        self.assertEqual(len(writes), 2)
        job.refresh_from_db()
        self.assertEqual(job.status, S.COMPLETED)
        self.assertEqual(self.output_files(), [job.output_file.name])

    def test_write_that_committed_before_the_error_is_not_repeated(self):
        # Simulated: the UPDATE commits but the reply is lost. (Not reproducible
        # with a real connection on demand, so the error is injected after a real write.)
        job = self.processing_job()
        writes, real_write = [], worker.write_if_processing

        def write_then_lose_reply(*args):
            writes.append(1)
            real_write(*args)
            raise OperationalError(2013, 'Lost connection to MySQL server during query')

        with mock.patch.object(worker, 'write_if_processing', write_then_lose_reply), \
                self.assertLogs('apps.generator.worker', level='INFO') as logs:
            process_job(job, client=FakeClient())
        self.assertTrue(any('first write had been recorded' in line for line in logs.output))
        self.assertEqual(len(writes), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, S.COMPLETED)
        self.assertEqual(self.output_files(), [job.output_file.name])

    def test_recovered_job_is_not_completed_and_only_its_new_file_is_deleted(self):
        other = self.make_completed_job()
        job = self.processing_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        job.input_file.save('in.png', ContentFile(PNG), save=True)
        # A file already at this job's usual output name is not this attempt's file either.
        existing = datetime.now().strftime(f'outputs/%Y/%m/{job.id}.mp4')  # as FileField names it
        (self.media_root / existing).parent.mkdir(parents=True, exist_ok=True)
        (self.media_root / existing).write_bytes(b'not this attempt')
        before = self.output_files()
        recoverer = self.thread()

        def recovered_then_disconnected():
            recoverer.call(lambda: recover_interrupted_jobs(now=timezone.now() + STALE))
            self.kill_own_connection()

        with mock.patch.object(worker, 'drop_broken_connection'), \
                self.assertLogs('apps.generator.worker', level='WARNING') as logs:
            process_job(job, client=FakeClient(during=recovered_then_disconnected))
        self.assertTrue(any('reconnecting to check the job' in line for line in logs.output))
        self.assertTrue(any('lease lost' in line for line in logs.output))
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code, job.output_file.name), (S.FAILED, 'worker_interrupted', ''))
        self.assertEqual(self.output_files(), before)  # the late file is gone, nothing else
        self.assertIn(other.output_file.name, before)
        self.assertEqual((self.media_root / existing).read_bytes(), b'not this attempt')
        self.assertTrue((self.media_root / job.input_file.name).exists())
        self.assertEqual(status_of(other), S.COMPLETED)

    def test_job_deleted_then_connection_lost_is_rechecked_and_discarded(self):
        # 3.5.1: the write fails on a dead connection; the re-check finds no job (it was
        # deleted by another connection), so nothing is retried and only the new file goes.
        other = self.make_completed_job()
        job = self.processing_job()
        before = self.output_files()
        deleter = self.thread()
        writes, real_write = [], worker.write_if_processing

        def counting_write(*args):
            writes.append(1)
            return real_write(*args)

        def deleted_then_disconnected():
            deleter.call(lambda: GenerationJob.objects.filter(pk=job.pk).delete())
            self.kill_own_connection()

        with mock.patch.object(worker, 'drop_broken_connection'), \
                mock.patch.object(worker, 'write_if_processing', counting_write), \
                self.assertLogs('apps.generator.worker', level='INFO') as logs:
            process_job(job, client=FakeClient(during=deleted_then_disconnected))  # must not raise
        self.assertTrue(any('reconnecting to check the job' in line for line in logs.output))
        self.assertTrue(any('no longer exists' in line for line in logs.output))
        self.assertFalse(any('could not record its result' in line for line in logs.output))
        self.assertEqual(len(writes), 1)  # never retried for a job that does not exist
        self.assertEqual(self.output_files(), before)
        self.assertTrue((self.media_root / other.output_file.name).exists())
        self.assertFalse(GenerationJob.objects.filter(pk=job.pk).exists())

    def test_late_worker_cannot_complete_a_job_recovered_by_another_connection(self):
        job = self.processing_job()
        recoverer = self.thread()
        with self.assertLogs('apps.generator.worker', level='WARNING'):
            process_job(job, client=FakeClient(
                during=lambda: recoverer.call(lambda: recover_interrupted_jobs(now=timezone.now() + STALE))))
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'worker_interrupted'))
        self.assertEqual(self.output_files(), [])

    def test_unknown_outcome_keeps_the_file_and_the_job_state(self):
        other = self.make_completed_job()
        job = self.processing_job()
        lost = OperationalError(2013, 'Lost connection to MySQL server during query')
        with mock.patch.object(worker, 'write_if_processing', side_effect=lost), \
                mock.patch.object(worker, 'recheck_and_retry', side_effect=lost), \
                self.assertLogs('apps.generator.worker', level='ERROR') as logs:
            process_job(job, client=FakeClient())  # must not raise
        message = next(line for line in logs.output if 'could not record its result' in line)
        self.assertIn('OperationalError 2013', message)
        saved = [name for name in self.output_files() if name != other.output_file.name]
        self.assertEqual(len(saved), 1)
        self.assertIn(saved[0], message)  # enough to find the kept file
        self.assertEqual(status_of(job), S.PROCESSING)  # untouched; recovery will fail it later
        self.assertEqual(status_of(other), S.COMPLETED)
        self.assertTrue((self.media_root / other.output_file.name).exists())
