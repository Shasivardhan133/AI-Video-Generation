"""3.5.7: status changes always start from the stored status, never from a stale copy.

transition_to() is a conditional UPDATE; save() of an existing row that writes
`status` locks the row and checks it first. Concurrency tests use several real
MySQL connections on the temporary test database.
"""

import logging
import threading
import time
from datetime import timedelta
from unittest import mock

from django.db import connection, transaction
from django.utils import timezone

from apps.generator.models import GenerationJob, InvalidJobTransition, JobTransitionError, StaleJobState
from apps.generator.worker import claim_next_job, expire_queued_jobs, finish_job, recover_interrupted_jobs

from .base import TempMediaTestCase, TempMediaTransactionTestCase

S = GenerationJob.Status
TEXT_URL = '/api/v1/generations/text-to-video/'


def stored(job):
    return GenerationJob.objects.filter(pk=job.pk).values().get()


def copy_of(job):
    return GenerationJob.objects.get(pk=job.pk)


class TransitionTests(TempMediaTestCase):
    def test_lifecycle_and_stored_values(self):
        job = self.make_job()
        job.transition_to(S.PROCESSING)
        self.assertEqual(stored(job)['status'], S.PROCESSING)
        self.assertIsNotNone(stored(job)['started_at'])
        job.transition_to(S.COMPLETED, output_file='outputs/2026/10/x.mp4', remote_job_id='mock-1')
        row = stored(job)
        self.assertEqual((row['status'], row['output_file'], row['remote_job_id']), (S.COMPLETED, 'outputs/2026/10/x.mp4', 'mock-1'))
        self.assertEqual((job.status, job.output_file.name, job.completed_at), (S.COMPLETED, 'outputs/2026/10/x.mp4', row['completed_at']))
        self.assertEqual(job.updated_at, row['updated_at'])
        queued = self.make_job()
        queued.fail('cancelled', 'You cancelled this video.')
        self.assertEqual((stored(queued)['status'], stored(queued)['error_code']), (S.FAILED, 'cancelled'))

    def test_invalid_transitions_write_nothing(self):
        for start, target in ((S.QUEUED, S.COMPLETED), (S.QUEUED, S.QUEUED), (S.PROCESSING, S.QUEUED),
                              (S.COMPLETED, S.PROCESSING), (S.COMPLETED, S.FAILED), (S.FAILED, S.QUEUED),
                              (S.FAILED, S.COMPLETED)):
            with self.subTest(f'{start} -> {target}'):
                job = self.make_job(status=start)
                before = stored(job)
                with self.assertRaises(InvalidJobTransition) as ctx:
                    job.transition_to(target)
                self.assertIsInstance(ctx.exception, ValueError)  # earlier callers catch ValueError
                self.assertEqual(stored(job), before)

    def test_claiming_still_works(self):
        job = self.make_job()
        claimed = claim_next_job()
        self.assertEqual((claimed.pk, claimed.status, stored(job)['status']), (job.pk, S.PROCESSING, S.PROCESSING))
        claimed.transition_to(S.COMPLETED, output_file='outputs/x.mp4')  # the claimed instance stays usable
        self.assertEqual(stored(job)['status'], S.COMPLETED)


class StaleTransitionTests(TempMediaTestCase):
    """A copy loaded earlier must not undo what another operation did meanwhile."""

    def setUp(self):
        super().setUp()
        # Recovery, expiry and cancellation log as they work; keep that off the console.
        quiet = mock.patch.object(logging.getLogger('apps'), 'handlers', [logging.NullHandler()])
        quiet.start()
        self.addCleanup(quiet.stop)

    def assertRefused(self, stale, target, expected_status, **fields):
        before = None
        if GenerationJob.objects.filter(pk=stale.pk).exists():
            before = stored(stale)
        with self.assertRaises(StaleJobState):
            stale.transition_to(target, **fields)
        if before is not None:
            self.assertEqual(stored(stale), before)
            self.assertEqual(stale.status, expected_status)  # refreshed to the stored truth

    def test_cancelled_job_is_never_started(self):
        job = self.make_job()
        stale = copy_of(job)
        GenerationJob.objects.filter(pk=job.pk, status=S.QUEUED).update(status=S.FAILED, error_code='cancelled')
        self.assertRefused(stale, S.PROCESSING, S.FAILED)
        self.assertEqual(stored(job)['error_code'], 'cancelled')

    def test_expired_job_is_never_started(self):
        job = self.make_job(created_at=timezone.now() - timedelta(hours=2))
        stale = copy_of(job)
        self.assertEqual(expire_queued_jobs(), 1)
        self.assertRefused(stale, S.PROCESSING, S.FAILED)
        self.assertEqual(stored(job)['error_code'], 'queue_timeout')

    def test_recovered_job_is_never_revived(self):
        job = self.make_job(status=S.PROCESSING, started_at=timezone.now())
        stale = copy_of(job)
        GenerationJob.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(recover_interrupted_jobs(), 1)
        self.assertRefused(stale, S.COMPLETED, S.FAILED, output_file='outputs/late.mp4')
        row = stored(job)
        self.assertEqual((row['error_code'], row['output_file']), ('worker_interrupted', ''))

    def test_completed_job_keeps_its_result(self):
        job = self.make_job(status=S.PROCESSING, started_at=timezone.now())
        stale = copy_of(job)
        self.assertTrue(finish_job(copy_of(job), S.COMPLETED, output_file='outputs/2026/10/done.mp4'))
        with self.assertRaises(StaleJobState):
            stale.fail('internal_error', 'late failure')
        row = stored(job)
        self.assertEqual((row['status'], row['output_file'], row['error_code']), (S.COMPLETED, 'outputs/2026/10/done.mp4', ''))

    def test_failed_job_stays_failed(self):
        job = self.make_job(status=S.PROCESSING, started_at=timezone.now())
        stale = copy_of(job)
        copy_of(job).fail('service_timeout', 'Too slow.')
        self.assertRefused(stale, S.COMPLETED, S.FAILED, output_file='outputs/late.mp4')
        self.assertEqual(stored(job)['error_code'], 'service_timeout')

    def test_second_start_of_the_same_job_is_refused(self):
        job = self.make_job()
        first, second = copy_of(job), copy_of(job)
        first.transition_to(S.PROCESSING)
        started = stored(job)['started_at']
        self.assertRefused(second, S.PROCESSING, S.PROCESSING)
        self.assertEqual(stored(job)['started_at'], started)

    def test_deleted_job_is_not_recreated(self):
        job = self.make_job()
        stale = copy_of(job)
        GenerationJob.objects.filter(pk=job.pk).delete()
        with self.assertRaisesMessage(StaleJobState, 'no longer exists'):
            stale.transition_to(S.PROCESSING)
        self.assertFalse(GenerationJob.objects.filter(pk=job.pk).exists())

    def test_cancelled_job_frees_the_owner_even_if_a_stale_copy_tries_to_start_it(self):
        with self.settings(GENERATION_MAX_ACTIVE_JOBS_PER_OWNER=1):  # the real one-active-job rule
            post = lambda: self.client.post(TEXT_URL, {'prompt': 'A calm lake'}, content_type='application/json')
            job_id = post().json()['job_id']
            stale = GenerationJob.objects.get(pk=job_id)
            self.assertEqual(self.client.post(f'/api/v1/generations/{job_id}/cancel/').status_code, 200)
            with self.assertRaises(StaleJobState):
                stale.transition_to(S.PROCESSING)
            self.assertEqual(post().status_code, 202)  # the cancelled job does not block the owner


class StaleSaveTests(TempMediaTestCase):
    """save() of an existing row may not overwrite a status changed by someone else."""

    def test_stale_full_save_cannot_undo_a_completed_job(self):
        # The audit's reproduction: a copy loaded while processing, saved after completion.
        job = self.make_job(status=S.PROCESSING, started_at=timezone.now())
        stale = copy_of(job)
        self.assertTrue(finish_job(copy_of(job), S.COMPLETED, output_file='outputs/2026/10/done.mp4'))
        stale.prompt_text = 'edited'
        with self.assertRaises(StaleJobState):
            stale.save()
        row = stored(job)
        self.assertEqual((row['status'], row['output_file'], row['prompt_text']),
                         (S.COMPLETED, 'outputs/2026/10/done.mp4', 'A lighthouse at dusk'))

    def test_stale_save_cannot_revive_cancelled_recovered_or_expired_jobs(self):
        cases = {
            'cancelled': (S.QUEUED, {'status': S.FAILED, 'error_code': 'cancelled'}),
            'recovered': (S.PROCESSING, {'status': S.FAILED, 'error_code': 'worker_interrupted'}),
            'expired': (S.QUEUED, {'status': S.FAILED, 'error_code': 'queue_timeout'}),
            'failed': (S.PROCESSING, {'status': S.FAILED, 'error_code': 'service_timeout'}),
        }
        for name, (start, change) in cases.items():
            with self.subTest(name):
                job = self.make_job(status=start)
                stale = copy_of(job)
                GenerationJob.objects.filter(pk=job.pk).update(**change)
                before = stored(job)
                for kwargs in ({}, {'update_fields': ['status']}, {'update_fields': ['status', 'prompt_text']}):
                    with self.assertRaises(StaleJobState):
                        stale.save(**kwargs)
                self.assertEqual(stored(job), before)

    def test_save_of_a_deleted_job_does_not_insert_it_again(self):
        job = self.make_job()
        stale = copy_of(job)
        GenerationJob.objects.filter(pk=job.pk).delete()
        with self.assertRaisesMessage(StaleJobState, 'no longer exists'):
            stale.save()
        self.assertFalse(GenerationJob.objects.filter(pk=job.pk).exists())

    def test_forbidden_status_change_through_save(self):
        job = self.make_completed_job()
        fresh = copy_of(job)
        fresh.status = S.PROCESSING
        with self.assertRaises(InvalidJobTransition):
            fresh.save()
        self.assertEqual(stored(job)['status'], S.COMPLETED)

    def test_allowed_and_unrelated_saves_still_work(self):
        job = copy_of(self.make_job())
        job.status = S.PROCESSING  # allowed: queued -> processing
        job.save()
        self.assertEqual(stored(job)['status'], S.PROCESSING)
        job.prompt_text = 'edited'
        job.save()  # same status: allowed
        self.assertEqual(stored(job)['prompt_text'], 'edited')

    def test_saves_that_do_not_write_status_are_not_blocked(self):
        job = self.make_job()
        stale = copy_of(job)
        GenerationJob.objects.filter(pk=job.pk).update(status=S.FAILED, error_code='cancelled')
        stale.prompt_text = 'note'
        stale.save(update_fields=['prompt_text'])
        row = stored(job)
        self.assertEqual((row['status'], row['prompt_text']), (S.FAILED, 'note'))

    def test_refreshing_makes_a_stale_copy_usable_again(self):
        job = self.make_job()
        stale = copy_of(job)
        GenerationJob.objects.filter(pk=job.pk).update(status=S.PROCESSING)
        with self.assertRaises(StaleJobState):
            stale.save()
        stale.refresh_from_db()
        stale.prompt_text = 'after refresh'
        stale.save()
        self.assertEqual((stored(job)['status'], stored(job)['prompt_text']), (S.PROCESSING, 'after refresh'))

    def test_new_jobs_and_file_saves_are_unaffected(self):
        job = self.make_completed_job()  # create() and FileField.save(save=True)
        self.assertEqual(stored(job)['status'], S.COMPLETED)
        self.assertTrue(job.output_file.name)


class ConcurrentTransitionTests(TempMediaTransactionTestCase):
    """Real races between separate MySQL connections."""

    def race(self, *callables):
        barrier = threading.Barrier(len(callables))
        results = [None] * len(callables)

        def run(index, func):
            try:
                barrier.wait(10)
                results[index] = ('ok', func())
            except JobTransitionError as exc:
                results[index] = ('refused', type(exc).__name__)
            except BaseException as exc:  # noqa: BLE001 - reported below
                results[index] = ('error', repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(i, f), daemon=True) for i, f in enumerate(callables)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        return results

    def test_two_stale_copies_start_the_job_once(self):
        job = self.make_job()
        copies = [copy_of(job) for _ in range(6)]
        results = self.race(*[lambda c=c: c.transition_to(S.PROCESSING) for c in copies])
        self.assertEqual(sorted(r[0] for r in results), ['ok'] + ['refused'] * 5, results)
        self.assertTrue(all(r[1] == 'StaleJobState' for r in results if r[0] == 'refused'))
        self.assertEqual(stored(job)['status'], S.PROCESSING)

    def test_completion_and_failure_racing_apply_exactly_one(self):
        job = self.make_job(status=S.PROCESSING, started_at=timezone.now())
        a, b = copy_of(job), copy_of(job)
        results = self.race(lambda: a.transition_to(S.COMPLETED, output_file='outputs/x.mp4'),
                            lambda: b.fail('service_timeout', 'Too slow.'))
        self.assertEqual(sorted(r[0] for r in results), ['ok', 'refused'], results)
        row = stored(job)
        if row['status'] == S.COMPLETED:
            self.assertEqual((row['output_file'], row['error_code']), ('outputs/x.mp4', ''))
        else:
            self.assertEqual((row['status'], row['output_file'], row['error_code']), (S.FAILED, '', 'service_timeout'))

    def test_cancellation_and_a_stale_start_racing(self):
        job = self.make_job()
        stale = copy_of(job)
        cancel = lambda: GenerationJob.objects.filter(pk=job.pk, status=S.QUEUED).update(
            status=S.FAILED, error_code='cancelled', completed_at=timezone.now())
        results = self.race(cancel, lambda: stale.transition_to(S.PROCESSING))
        row = stored(job)
        if results[0] == ('ok', 1):
            self.assertEqual(results[1], ('refused', 'StaleJobState'))
            self.assertEqual((row['status'], row['error_code']), (S.FAILED, 'cancelled'))
        else:
            self.assertEqual(results, [('ok', 0), ('ok', None)])
            self.assertEqual(row['status'], S.PROCESSING)

    def test_stale_save_waits_for_the_row_lock_and_then_refuses(self):
        job = self.make_job(status=S.PROCESSING, started_at=timezone.now())
        stale = copy_of(job)
        locked, release = threading.Event(), threading.Event()

        def complete_while_holding_the_lock():
            try:
                with transaction.atomic():
                    GenerationJob.objects.select_for_update().get(pk=job.pk)
                    GenerationJob.objects.filter(pk=job.pk).update(status=S.COMPLETED, output_file='outputs/x.mp4')
                    locked.set()
                    release.wait(30)
            finally:
                connection.close()

        holder = threading.Thread(target=complete_while_holding_the_lock, daemon=True)
        holder.start()
        self.assertTrue(locked.wait(10))
        outcome = {}

        def stale_save():
            try:
                stale.prompt_text = 'edited'
                stale.save()
                outcome['result'] = 'saved'
            except StaleJobState:
                outcome['result'] = 'refused'
            finally:
                connection.close()

        saver = threading.Thread(target=stale_save, daemon=True)
        saver.start()
        time.sleep(0.5)
        self.assertTrue(saver.is_alive())  # waiting for the row lock
        release.set()
        holder.join(10)
        saver.join(10)
        self.assertEqual(outcome, {'result': 'refused'})
        row = stored(job)
        self.assertEqual((row['status'], row['output_file'], row['prompt_text']),
                         (S.COMPLETED, 'outputs/x.mp4', 'A lighthouse at dusk'))
