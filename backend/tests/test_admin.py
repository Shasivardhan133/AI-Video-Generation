"""3.5.2: the generation job admin is read-only.

Jobs are created by the API and changed only by the worker. The admin can list,
search, filter and inspect them, but cannot add jobs or change any field, so a
stale admin form can never undo a worker's result.
"""

import hashlib

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, RequestFactory, override_settings

from apps.generator.admin import GenerationJobAdmin
from apps.generator.models import GenerationJob

from .base import TempMediaTestCase

S = GenerationJob.Status
LIST_URL = '/admin/generator/generationjob/'
ADD_URL = f'{LIST_URL}add/'
SENSITIVE = ('status', 'input_file', 'output_file', 'user', 'session_key', 'remote_job_id',
             'error_code', 'error_message', 'request_id', 'settings', 'prompt_text', 'mode')


def change_url(job):
    return f'{LIST_URL}{job.pk}/change/'


# Users are created in every test; the default password hasher is deliberately slow.
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AdminTestCase(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = get_user_model().objects.create_superuser('root', 'root@example.invalid', 'unused-test-password')
        self.admin_client = Client()
        self.admin_client.force_login(self.superuser)
        self.guest_key = 'g' * 32
        self.job = self.make_completed_job(self.guest_key, prompt_text='A lighthouse at dusk', request_id='req-7')
        self.output_name = self.job.output_file.name

    def assertUnchanged(self, job=None):
        job = job or self.job
        stored = GenerationJob.objects.get(pk=job.pk)
        self.assertEqual((stored.status, stored.output_file.name, stored.session_key, stored.user_id, stored.prompt_text),
                         (S.COMPLETED, self.output_name, self.guest_key, None, 'A lighthouse at dusk'))

    def tampering_post(self):
        return {
            'status': S.PROCESSING, 'output_file': '', 'input_file': 'inputs/other.png', 'user': self.superuser.pk,
            'session_key': 'h' * 32, 'remote_job_id': 'x', 'error_code': 'x', 'error_message': 'x',
            'prompt_text': 'edited', 'mode': GenerationJob.Mode.IMAGE_TO_VIDEO, 'settings': '{}', '_save': 'Save',
        }


class ReadOnlyAdminTests(AdminTestCase):
    def test_admin_class_has_no_add_or_change_permission(self):
        model_admin = admin.site._registry[GenerationJob]
        self.assertIsInstance(model_admin, GenerationJobAdmin)
        request = RequestFactory().get('/')
        request.user = self.superuser
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, self.job))
        self.assertTrue(model_admin.has_view_permission(request, self.job))

    def test_every_shown_field_is_read_only(self):
        model_admin = admin.site._registry[GenerationJob]
        request = RequestFactory().get('/')
        request.user = self.superuser
        shown = {name for _, options in model_admin.get_fieldsets(request, self.job) for name in options['fields']}
        self.assertEqual(shown - set(model_admin.get_readonly_fields(request, self.job)), set())
        self.assertNotIn('session_key', shown)  # shown only as a fingerprint

    def test_change_page_is_view_only(self):
        response = self.admin_client.get(change_url(self.job))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for field in SENSITIVE:
            self.assertNotIn(f'name="{field}"', html, field)  # no form inputs
        self.assertNotIn('name="_save"', html)
        self.assertNotIn('type="file"', html)
        # Useful information is still shown.
        for text in (str(self.job.id), 'A lighthouse at dusk', self.output_name, 'req-7', 'wan2.1-t2v-1.3b-480p'):
            self.assertIn(text, html)
        fingerprint = hashlib.sha256(self.guest_key.encode()).hexdigest()[:8]
        self.assertIn(f'Guest session {fingerprint}', html)
        self.assertNotIn(self.guest_key, html)  # the session key itself is never shown
        self.assertNotIn(f'href="/media/{self.output_name}"', html)  # files are never linked

    def test_posting_changes_is_refused_and_nothing_changes(self):
        response = self.admin_client.post(change_url(self.job), self.tampering_post())
        self.assertEqual(response.status_code, 403)
        self.assertUnchanged()

    def test_stale_form_cannot_undo_the_workers_result(self):
        # The form is opened while the job is processing; the worker then completes it.
        job = self.make_job(self.guest_key, prompt_text='A lighthouse at dusk', status=S.PROCESSING)
        self.assertEqual(self.admin_client.get(change_url(job)).status_code, 200)
        GenerationJob.objects.filter(pk=job.pk).update(status=S.COMPLETED, output_file=self.output_name)
        response = self.admin_client.post(change_url(job), {**self.tampering_post(), 'status': S.PROCESSING})
        self.assertEqual(response.status_code, 403)
        self.assertUnchanged(job)

    def test_jobs_cannot_be_added(self):
        count = GenerationJob.objects.count()
        self.assertEqual(self.admin_client.get(ADD_URL).status_code, 403)
        self.assertEqual(self.admin_client.post(ADD_URL, self.tampering_post()).status_code, 403)
        self.assertEqual(GenerationJob.objects.count(), count)
        self.assertNotIn(f'href="{ADD_URL}"', self.admin_client.get(LIST_URL).content.decode())

    def test_model_permissions_do_not_reenable_editing(self):
        staff = get_user_model().objects.create_user('staff', password='unused-test-password', is_staff=True)
        staff.user_permissions.set(Permission.objects.filter(content_type__app_label='generator'))
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.get(change_url(self.job)).status_code, 200)  # view permission still works
        self.assertEqual(client.post(change_url(self.job), self.tampering_post()).status_code, 403)
        self.assertEqual(client.get(ADD_URL).status_code, 403)
        self.assertUnchanged()

    def test_staff_without_view_permission_sees_nothing(self):
        staff = get_user_model().objects.create_user('nobody', password='unused-test-password', is_staff=True)
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.get(change_url(self.job)).status_code, 403)
        self.assertEqual(client.get(LIST_URL).status_code, 403)


class AdminInspectionTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.alice = get_user_model().objects.create_user('alice', password='unused-test-password')
        self.failed = self.make_job(user=self.alice, prompt_text='A desert caravan', status=S.FAILED,
                                    error_code='service_timeout', error_message='Too slow.', remote_job_id='colab-42')

    def listed_ids(self, **params):
        response = self.admin_client.get(LIST_URL, params)
        self.assertEqual(response.status_code, 200)
        return {str(job.pk) for job in response.context['cl'].result_list}

    def test_list_shows_jobs_with_owner(self):
        response = self.admin_client.get(LIST_URL)
        html = response.content.decode()
        self.assertEqual(self.listed_ids(), {str(self.job.pk), str(self.failed.pk)})
        self.assertIn('alice', html)
        self.assertIn('Guest session', html)
        self.assertNotIn(self.guest_key, html)

    def test_search(self):
        self.assertEqual(self.listed_ids(q='caravan'), {str(self.failed.pk)})
        self.assertEqual(self.listed_ids(q='colab-42'), {str(self.failed.pk)})
        self.assertEqual(self.listed_ids(q='req-7'), {str(self.job.pk)})

    def test_filters(self):
        self.assertEqual(self.listed_ids(status__exact=S.FAILED), {str(self.failed.pk)})
        self.assertEqual(self.listed_ids(mode__exact=GenerationJob.Mode.TEXT_TO_VIDEO), {str(self.job.pk), str(self.failed.pk)})

    def test_failed_job_details_are_viewable(self):
        html = self.admin_client.get(change_url(self.failed)).content.decode()
        for text in ('service_timeout', 'Too slow.', 'colab-42', 'alice'):
            self.assertIn(text, html)

    def test_viewing_never_changes_a_job(self):
        before = GenerationJob.objects.filter(pk=self.job.pk).values().get()
        self.admin_client.get(LIST_URL)
        self.admin_client.get(change_url(self.job))
        self.assertEqual(GenerationJob.objects.filter(pk=self.job.pk).values().get(), before)


class AdminDeleteTests(AdminTestCase):
    """Deleting is the one admin action allowed; files stay (retention policy)."""

    def delete_url(self):
        return f'{LIST_URL}{self.job.pk}/delete/'

    def test_superuser_can_delete_a_job_and_its_files_are_kept(self):
        video = self.media_root / self.output_name
        response = self.admin_client.post(self.delete_url(), {'post': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(GenerationJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(video.is_file(), 'the video must not be deleted with its row')

    def test_staff_without_delete_permission_cannot_delete(self):
        staff = get_user_model().objects.create_user('viewer', password='unused-test-password', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename='view_generationjob'))
        viewer = Client()
        viewer.force_login(staff)
        self.assertEqual(viewer.post(self.delete_url(), {'post': 'yes'}).status_code, 403)
        self.assertUnchanged()
