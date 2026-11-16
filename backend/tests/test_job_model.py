"""GenerationJob lifecycle rules."""

from apps.generator.models import GenerationJob

from .base import TempMediaTestCase

S = GenerationJob.Status


class TransitionTests(TempMediaTestCase):
    def test_new_job_is_queued(self):
        self.assertEqual(self.make_job().status, S.QUEUED)

    def test_queued_to_processing_to_completed(self):
        job = self.make_job()
        job.transition_to(S.PROCESSING)
        job.refresh_from_db()
        self.assertEqual(job.status, S.PROCESSING)
        self.assertIsNotNone(job.started_at)
        self.assertIsNone(job.completed_at)

        job.transition_to(S.COMPLETED, remote_job_id='mock-1')
        job.refresh_from_db()
        self.assertEqual(job.status, S.COMPLETED)
        self.assertEqual(job.remote_job_id, 'mock-1')
        self.assertIsNotNone(job.completed_at)
        self.assertGreaterEqual(job.completed_at, job.started_at)

    def test_queued_and_processing_can_fail(self):
        for path in ([], [S.PROCESSING]):
            job = self.make_job()
            for step in path:
                job.transition_to(step)
            job.fail('service_unavailable', 'The AI service is unavailable.')
            job.refresh_from_db()
            self.assertEqual(job.status, S.FAILED)
            self.assertEqual(job.error_code, 'service_unavailable')
            self.assertEqual(job.error_message, 'The AI service is unavailable.')
            self.assertIsNotNone(job.completed_at)

    def test_fail_truncates_long_messages(self):
        job = self.make_job()
        job.fail('internal_error', 'x' * 900)
        job.refresh_from_db()
        self.assertEqual(len(job.error_message), 500)

    def test_disallowed_transitions_raise_and_leave_job_unchanged(self):
        disallowed = {
            S.QUEUED: [S.QUEUED, S.COMPLETED],
            S.PROCESSING: [S.QUEUED, S.PROCESSING],
            S.COMPLETED: [S.QUEUED, S.PROCESSING, S.FAILED, S.COMPLETED],
            S.FAILED: [S.QUEUED, S.PROCESSING, S.COMPLETED, S.FAILED],
        }
        for start, targets in disallowed.items():
            for target in targets:
                with self.subTest(f'{start} -> {target}'):
                    job = self.make_job(status=start)
                    with self.assertRaises(ValueError):
                        job.transition_to(target)
                    job.refresh_from_db()
                    self.assertEqual(job.status, start)

    def test_video_available_needs_completed_status_and_file(self):
        self.assertFalse(self.make_job().video_available)
        self.assertFalse(self.make_job(status=S.COMPLETED).video_available)
        self.assertTrue(self.make_completed_job().video_available)
