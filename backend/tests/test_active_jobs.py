"""3.5.3: at most one active (queued or processing) generation job per owner.

These tests enforce the real limit (relax_active_job_limit = False). The
concurrency tests send simultaneous requests from several threads, each with its
own MySQL connection, against the temporary test database.
"""

import threading
import time
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings

from apps.generator import active_jobs
from apps.generator.active_jobs import lock_name, owner_lock
from apps.generator.models import GenerationJob
from apps.generator.worker import claim_next_job, recover_interrupted_jobs

from .base import JPEG, PNG, TempMediaTestCase, TempMediaTransactionTestCase

S = GenerationJob.Status
TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'
FAST_HASHER = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


def post_text(client, prompt='A calm lake'):
    return client.post(TEXT_URL, {'prompt': prompt}, content_type='application/json')


def post_image(client, prompt='Animate it', image=PNG, name='photo.png'):
    return client.post(IMAGE_URL, {'image': SimpleUploadedFile(name, image, content_type='image/png'), 'prompt': prompt})


def is_free(owner):
    with connection.cursor() as cursor:
        cursor.execute('SELECT IS_FREE_LOCK(%s)', [lock_name(owner)])
        return cursor.fetchone()[0] == 1


class ActiveJobHelpers:
    relax_active_job_limit = False

    def stored_inputs(self):
        inputs = self.media_root / 'inputs'
        return sorted(p for p in inputs.rglob('*') if p.is_file()) if inputs.exists() else []

    def assertInProgress(self, response):
        error = self.assertApiError(response, 409, 'generation_in_progress')
        self.assertEqual(error['message'], 'Another video is still being generated. Wait for it to finish, then try again.')
        self.assertEqual(response['Cache-Control'], 'private, no-store')


class GuestActiveJobTests(ActiveJobHelpers, TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(settings.GENERATION_MAX_ACTIVE_JOBS_PER_OWNER, 1)  # the real limit

    def test_second_request_while_queued_or_processing_is_rejected(self):
        for active in (S.QUEUED, S.PROCESSING):
            for name, send in (('text', post_text), ('image', post_image)):
                with self.subTest(active=active, mode=name):
                    GenerationJob.objects.all().delete()
                    self.assertEqual(post_text(self.client, 'first').status_code, 202)
                    GenerationJob.objects.update(status=active)
                    self.assertInProgress(send(self.client))
                    self.assertEqual(GenerationJob.objects.count(), 1)
                    self.assertEqual(GenerationJob.objects.get().prompt_text, 'first')

    def test_an_active_image_job_blocks_a_text_request(self):
        self.assertEqual(post_image(self.client).status_code, 202)
        self.assertInProgress(post_text(self.client))
        self.assertEqual(GenerationJob.objects.count(), 1)

    def test_rejected_image_upload_stores_nothing(self):
        self.assertEqual(post_text(self.client).status_code, 202)
        for image in (PNG, JPEG):
            self.assertInProgress(post_image(self.client, image=image, name='second.png'))
        self.assertEqual(self.stored_inputs(), [])
        self.assertEqual(GenerationJob.objects.count(), 1)
        self.assertFalse(GenerationJob.objects.get().input_file)

    def test_finished_jobs_do_not_block(self):
        for finished in (S.COMPLETED, S.FAILED):
            with self.subTest(finished=finished):
                GenerationJob.objects.update(status=finished)
                self.assertEqual(post_text(self.client).status_code, 202)
        GenerationJob.objects.update(status=S.COMPLETED)
        response = post_image(self.client)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(self.stored_inputs()), 1)

    def test_the_next_request_is_accepted_once_the_job_finishes(self):
        first = post_text(self.client).json()['job_id']
        self.assertInProgress(post_text(self.client))
        GenerationJob.objects.filter(pk=first).update(status=S.PROCESSING)
        self.assertInProgress(post_text(self.client))
        GenerationJob.objects.filter(pk=first).update(status=S.COMPLETED)
        self.assertEqual(post_text(self.client).status_code, 202)

    def test_other_guest_sessions_are_not_blocked(self):
        self.assertEqual(post_text(self.client).status_code, 202)
        other = Client()
        self.assertEqual(post_text(other).status_code, 202)
        self.assertInProgress(post_text(other))
        self.assertEqual(GenerationJob.objects.count(), 2)
        self.assertEqual(GenerationJob.objects.values('session_key').distinct().count(), 2)

    def test_rejection_keeps_the_session_and_history(self):
        job_id = post_text(self.client).json()['job_id']
        key = self.client.session.session_key
        self.assertInProgress(post_text(self.client))
        self.assertEqual(self.client.session.session_key, key)
        self.assertEqual([j['job_id'] for j in self.client.get('/api/v1/generations/').json()['results']], [job_id])

    def test_validation_errors_are_still_reported_first(self):
        self.assertEqual(post_text(self.client).status_code, 202)
        self.assertApiError(post_text(self.client, prompt=''), 400, 'validation_error', fields=['prompt'])
        response = self.client.post(TEXT_URL, {'prompt': 'x', 'unknown': 1}, content_type='application/json')
        self.assertApiError(response, 400, 'unknown_field', fields=['unknown'])

    def test_csrf_is_still_checked_first(self):
        browser = Client(enforce_csrf_checks=True)
        token = browser.get('/api/v1/csrf/').json()['csrf_token']
        headers = {'X-CSRFToken': token}
        self.assertEqual(browser.post(TEXT_URL, {'prompt': 'a'}, content_type='application/json', headers=headers).status_code, 202)
        response = browser.post(TEXT_URL, {'prompt': 'b'}, content_type='application/json')  # no token
        self.assertApiError(response, 403, 'forbidden')
        self.assertInProgress(browser.post(TEXT_URL, {'prompt': 'b'}, content_type='application/json', headers=headers))
        self.assertEqual(GenerationJob.objects.count(), 1)

    def test_lock_is_released_after_every_outcome(self):
        response = post_text(self.client)
        owner = f'session:{self.client.session.session_key}'
        self.assertEqual(response.status_code, 202)
        self.assertTrue(is_free(owner))
        self.assertInProgress(post_text(self.client))
        self.assertTrue(is_free(owner))
        GenerationJob.objects.update(status=S.COMPLETED)
        with mock.patch.object(GenerationJob.objects, 'create', side_effect=RuntimeError('boom')), \
                self.assertLogs('apps.generator.exceptions', level='ERROR'):
            self.assertApiError(post_text(self.client), 500, 'internal_error')
        self.assertTrue(is_free(owner))

    def test_lock_name_never_contains_the_session_key(self):
        key = 'k' * 32
        name = lock_name(f'session:{key}')
        self.assertNotIn(key, name)
        self.assertLessEqual(len(name), 64)  # MySQL's limit
        self.assertNotEqual(name, lock_name('session:' + 'j' * 32))
        self.assertNotEqual(lock_name('user:1'), lock_name('user:2'))


@FAST_HASHER
class SignedInActiveJobTests(ActiveJobHelpers, TempMediaTestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.alice = User.objects.create_user('alice', password='unused-test-password')
        self.bob = User.objects.create_user('bob', password='unused-test-password')

    def signed_in(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_one_active_job_per_user_across_browsers(self):
        laptop, phone = self.signed_in(self.alice), self.signed_in(self.alice)
        self.assertEqual(post_text(laptop).status_code, 202)
        self.assertInProgress(post_text(phone))
        self.assertInProgress(post_image(phone))
        self.assertEqual(self.stored_inputs(), [])
        self.assertEqual(GenerationJob.objects.filter(user=self.alice).count(), 1)

    def test_other_users_and_guests_are_not_blocked(self):
        self.assertEqual(post_text(self.signed_in(self.alice)).status_code, 202)
        self.assertEqual(post_text(self.signed_in(self.bob)).status_code, 202)
        self.assertEqual(post_text(Client()).status_code, 202)
        self.assertEqual(GenerationJob.objects.count(), 3)

    def test_a_guest_job_in_the_same_browser_belongs_to_another_owner(self):
        client = self.signed_in(self.alice)
        self.make_job(client.session.session_key)  # a guest job (no user) in this session
        self.assertEqual(post_text(client).status_code, 202)
        self.assertInProgress(post_text(client))


class ConcurrentSubmissionTests(ActiveJobHelpers, TempMediaTransactionTestCase):
    THREADS = 8

    def shared_session(self):
        session = SessionStore()
        session.create()
        return session.session_key

    def client_for(self, session_key):
        client = Client()
        if session_key:
            client.cookies[settings.SESSION_COOKIE_NAME] = session_key
        return client

    def race(self, senders):
        """Start every sender at the same moment, each on its own thread and connection."""
        barrier = threading.Barrier(len(senders))
        results = [None] * len(senders)

        def run(index, send):
            try:
                barrier.wait(timeout=10)
                results[index] = send()
            except BaseException as exc:  # noqa: BLE001 - reported below
                results[index] = exc
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(i, send), daemon=True) for i, send in enumerate(senders)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    def test_simultaneous_text_requests_from_one_session_create_one_job(self):
        key = self.shared_session()
        responses = self.race([lambda: post_text(self.client_for(key), f'p{i}') for i in range(self.THREADS)])
        codes = sorted(r.status_code for r in responses)
        self.assertEqual(codes, [202] + [409] * (self.THREADS - 1))
        for response in responses:
            if response.status_code == 409:
                self.assertInProgress(response)
        self.assertEqual(GenerationJob.objects.filter(session_key=key).count(), 1)
        self.assertTrue(is_free(f'session:{key}'))

    def test_simultaneous_image_requests_store_one_upload(self):
        key = self.shared_session()
        responses = self.race([lambda: post_image(self.client_for(key)) for _ in range(self.THREADS)])
        self.assertEqual(sorted(r.status_code for r in responses), [202] + [409] * (self.THREADS - 1))
        job = GenerationJob.objects.get(session_key=key)
        self.assertEqual(self.stored_inputs(), [self.media_root / job.input_file.name])

    def test_simultaneous_mixed_requests_create_one_job(self):
        key = self.shared_session()
        senders = [(lambda: post_text(self.client_for(key))) if i % 2 else (lambda: post_image(self.client_for(key)))
                   for i in range(self.THREADS)]
        responses = self.race(senders)
        self.assertEqual(sorted(r.status_code for r in responses), [202] + [409] * (self.THREADS - 1))
        self.assertEqual(GenerationJob.objects.count(), 1)
        self.assertLessEqual(len(self.stored_inputs()), 1)

    @FAST_HASHER
    def test_simultaneous_requests_from_one_user_in_several_browsers(self):
        user = get_user_model().objects.create_user('carol', password='unused-test-password')
        clients = []
        for _ in range(self.THREADS):
            client = Client()
            client.force_login(user)
            clients.append(client)
        responses = self.race([lambda c=c: post_text(c) for c in clients])
        self.assertEqual(sorted(r.status_code for r in responses), [202] + [409] * (self.THREADS - 1))
        self.assertEqual(GenerationJob.objects.filter(user=user).count(), 1)

    def test_different_owners_are_not_serialized(self):
        keys = [self.shared_session() for _ in range(self.THREADS)]
        responses = self.race([lambda k=k: post_text(self.client_for(k)) for k in keys])
        self.assertEqual([r.status_code for r in responses], [202] * self.THREADS)
        self.assertEqual(GenerationJob.objects.count(), self.THREADS)

    def test_a_request_waiting_too_long_for_the_lock_is_rejected(self):
        key = self.shared_session()
        owner = f'session:{key}'
        held, release = threading.Event(), threading.Event()

        def hold_lock():
            try:
                with owner_lock(owner):  # another request of this owner, still in progress
                    held.set()
                    release.wait(30)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        self.assertTrue(held.wait(10))
        try:
            with mock.patch.object(active_jobs, 'OWNER_LOCK_WAIT_SECONDS', 1):
                started = time.monotonic()
                response = post_text(self.client_for(key))
                waited = time.monotonic() - started
        finally:
            release.set()
            holder.join(10)
        self.assertInProgress(response)
        self.assertLess(waited, 5)
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertTrue(is_free(owner))

    def test_worker_claiming_and_recovery_ignore_owner_locks(self):
        key = self.shared_session()
        self.assertEqual(post_text(self.client_for(key)).status_code, 202)
        held, release, outcome = threading.Event(), threading.Event(), {}

        def hold_lock():
            try:
                with owner_lock(f'session:{key}'):
                    held.set()
                    release.wait(30)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        self.assertTrue(held.wait(10))
        try:
            outcome['claimed'] = claim_next_job()
            outcome['recovered'] = recover_interrupted_jobs()
        finally:
            release.set()
            holder.join(10)
        self.assertIsNotNone(outcome['claimed'])
        self.assertEqual(outcome['claimed'].status, S.PROCESSING)
        self.assertEqual(outcome['recovered'], 0)
