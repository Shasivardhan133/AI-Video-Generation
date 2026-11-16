"""3.8: the full ownership matrix and CSRF on the read endpoints.

Four owners: two guests (browser sessions) and two signed-in users. Every
requester other than the owner gets the same 404 as for a missing job, on the
detail, video, download and cancel endpoints, and never sees the job in the
history list; the job and its video are left unchanged.
"""

from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.generator.models import GenerationJob

from .base import MP4, TempMediaTestCase

S = GenerationJob.Status
LIST_URL = '/api/v1/generations/'
FAST_HASHER = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


def urls(job):
    detail = f'/api/v1/generations/{job.id}/'
    return {'detail': detail, 'video': f'{detail}video/', 'download': f'{detail}video/?download=1',
            'cancel': f'{detail}cancel/'}


@FAST_HASHER
class OwnershipMatrixTests(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.clients = {'guest A': Client(), 'guest B': Client(), 'alice': Client(), 'bob': Client()}
        self.clients['alice'].force_login(User.objects.create_user('alice', password='unused-test-password'))
        self.clients['bob'].force_login(User.objects.create_user('bob', password='unused-test-password'))
        self.jobs = {}
        for name, client in self.clients.items():
            owner = ({'user': User.objects.get(username=name)} if name in ('alice', 'bob')
                     else {'session_key': self.session_key(client)})
            self.jobs[name] = {'queued': self.make_job(**owner), 'completed': self.make_completed_job(**owner)}

    def read(self, client, url):
        response = client.get(url)
        if response.streaming:
            response.body = b''.join(response.streaming_content)  # lets the test client close the file
        return response

    def test_only_the_owner_reaches_a_job(self):
        for owner, jobs in self.jobs.items():
            for requester, client in self.clients.items():
                with self.subTest(owner=owner, requester=requester):
                    queued, completed = urls(jobs['queued']), urls(jobs['completed'])
                    if requester == owner:
                        self.assertEqual(self.read(client, completed['detail']).status_code, 200)
                        self.assertEqual(self.read(client, completed['video']).body, MP4)
                        continue
                    for url in (completed['detail'], completed['video'], completed['download'], queued['detail']):
                        response = self.read(client, url)
                        error = self.assertApiError(response, 404, 'not_found')
                        self.assertNotIn(str(jobs['completed'].id), error['message'])
                    self.assertApiError(client.post(queued['cancel']), 404, 'not_found')
        for jobs in self.jobs.values():
            self.assertEqual(GenerationJob.objects.get(pk=jobs['queued'].pk).status, S.QUEUED)  # nobody cancelled it

    def test_history_lists_only_the_requesters_own_jobs(self):
        for name, client in self.clients.items():
            with self.subTest(requester=name):
                listed = {item['job_id'] for item in client.get(LIST_URL, {'page_size': 50}).json()['results']}
                self.assertEqual(listed, {str(job.id) for job in self.jobs[name].values()})

    def test_a_visitor_without_a_session_sees_nothing(self):
        stranger = Client()
        self.assertEqual(stranger.get(LIST_URL).json()['results'], [])
        for jobs in self.jobs.values():
            self.assertApiError(self.read(stranger, urls(jobs['completed'])['video']), 404, 'not_found')


class ReadEndpointCsrfTests(TempMediaTestCase):
    """Unsafe methods on the read endpoints, from a browser that enforces CSRF."""

    def test_unsafe_methods_need_a_token_and_are_then_refused(self):
        browser = Client(enforce_csrf_checks=True)
        token = browser.get('/api/v1/csrf/').json()['csrf_token']
        job = self.make_completed_job(self.session_key(browser))
        before = GenerationJob.objects.filter(pk=job.pk).values().get()
        for url in (LIST_URL, urls(job)['detail'], urls(job)['video']):
            for method in ('post', 'put', 'patch', 'delete'):
                with self.subTest(url=url, method=method):
                    self.assertApiError(getattr(browser, method)(url), 403, 'forbidden')
                    response = getattr(browser, method)(url, headers={'X-CSRFToken': token})
                    self.assertApiError(response, 405, 'method_not_allowed')
        self.assertEqual(GenerationJob.objects.filter(pk=job.pk).values().get(), before)
        self.assertTrue((self.media_root / job.output_file.name).is_file())
