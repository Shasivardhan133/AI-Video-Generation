"""C5: request fields. Unknown fields are rejected, settings accept only the
documented formats, and settings values must have the right JSON type."""

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.generator.models import GenerationJob

from .base import PNG, TEXT_SETTINGS, TempMediaTestCase

TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'


def png():
    return SimpleUploadedFile('photo.png', PNG, content_type='image/png')


class FieldTestCase(TempMediaTestCase):
    def post_json(self, data, url=TEXT_URL, client=None):
        body = data if isinstance(data, str) else json.dumps(data)
        return (client or self.client).post(url, body, content_type='application/json')

    def post_form(self, data, url=IMAGE_URL, client=None):
        return (client or self.client).post(url, data)  # multipart

    def assertRejected(self, response, code, fields):
        error = self.assertApiError(response, 400, code, fields=fields)
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertFalse((self.media_root / 'inputs').exists())  # no upload stored
        return error

    def assertUnknown(self, response, path, names):
        """`path` is where the unknown names sit: [] for top level, ['settings'] inside settings."""
        error = self.assertRejected(response, 'unknown_field', fields=[path[0]] if path else names)
        fields = error['fields']
        for key in path:
            fields = fields[key]
        self.assertEqual(sorted(fields), sorted(names))
        for name in names:
            self.assertEqual(fields[name], ['Unknown field.'])
        self.assertEqual(error['message'], 'Unknown field.')
        return error


class UnknownTopLevelFieldTests(FieldTestCase):
    def test_text_to_video_json(self):
        self.assertUnknown(self.post_json({'prompt': 'A cat', 'promt': 'typo'}), [], ['promt'])
        self.assertUnknown(self.post_json({'prompt': 'A cat', 'duration': 5, 'model': 'x'}), [], ['duration', 'model'])

    def test_text_to_video_form_and_multipart(self):
        self.assertUnknown(self.client.post(TEXT_URL, {'prompt': 'A cat', 'extra': '1'}), [], ['extra'])
        # An image sent to text-to-video used to be dropped silently.
        self.assertUnknown(self.post_form({'prompt': 'A cat', 'image': png()}, url=TEXT_URL), [], ['image'])

    def test_image_to_video_multipart(self):
        self.assertUnknown(self.post_form({'image': png(), 'prompt': 'A cat', 'mode': 'fast'}), [], ['mode'])
        self.assertUnknown(self.post_form({'image': png(), 'image2': png()}), [], ['image2'])

    def test_reported_before_other_errors(self):
        # Unknown fields are checked first, so the code is stable even if other fields are wrong too.
        self.assertUnknown(self.post_json({'extra': 1}), [], ['extra'])
        self.assertUnknown(self.post_form({'extra': 'x'}), [], ['extra'])

    def test_csrf_form_field_is_allowed(self):
        browser = Client(enforce_csrf_checks=True)
        token = browser.get('/api/v1/csrf/').json()['csrf_token']
        response = self.post_form({'image': png(), 'csrfmiddlewaretoken': token}, client=browser)
        self.assertEqual(response.status_code, 202, response.content)
        response = browser.post(TEXT_URL, {'prompt': 'A cat', 'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 202, response.content)


class UnknownSettingTests(FieldTestCase):
    def test_text_to_video(self):
        self.assertUnknown(self.post_json({'prompt': 'a', 'settings': {'duration': 5}}), ['settings'], ['duration'])
        self.assertUnknown(self.post_json({'prompt': 'a', 'settings': {'widht': 480, 'height': 832, 'fsp': 16}}),
                           ['settings'], ['fsp', 'widht'])

    def test_image_to_video(self):
        response = self.post_form({'image': png(), 'settings': json.dumps({'frame_count': 33, 'motion': 'high'})})
        self.assertUnknown(response, ['settings'], ['motion'])


class SettingsFormatTests(FieldTestCase):
    def test_json_body_needs_an_object(self):
        for settings in ('{"frame_count": 33}', '', 'not json', 5, 1.5, True, ['a'], None):
            with self.subTest(settings=settings):
                error = self.assertRejected(self.post_json({'prompt': 'a', 'settings': settings}),
                                            'validation_error', fields=['settings'])
                expected = 'This field may not be null.' if settings is None else 'Settings must be a JSON object.'
                self.assertEqual(error['fields']['settings'], [expected])

    def test_form_body_needs_the_json_text_of_an_object(self):
        cases = {'{bad json': 'Settings must be valid JSON.', '[1, 2]': 'Settings must be a JSON object.',
                 '"text"': 'Settings must be a JSON object.', '5': 'Settings must be a JSON object.',
                 'null': 'Settings must be a JSON object.', '[' * 100_000: 'Settings must be valid JSON.'}
        for text, message in cases.items():
            with self.subTest(text=text[:20]):
                error = self.assertRejected(self.post_form({'image': png(), 'settings': text}),
                                            'validation_error', fields=['settings'])
                self.assertEqual(error['fields']['settings'], [message])

    def test_settings_sent_as_a_file_is_rejected(self):
        response = self.post_form({'image': png(), 'settings': SimpleUploadedFile('s.json', b'{}', 'application/json')})
        self.assertRejected(response, 'validation_error', fields=['settings'])

    def test_missing_or_empty_settings_use_defaults(self):
        response = self.post_json({'prompt': 'a'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).settings, TEXT_SETTINGS)
        for data in ({'image': png()}, {'image': png(), 'settings': ''}, {'image': png(), 'settings': '  '},
                     {'image': png(), 'settings': '{}'}):
            with self.subTest(settings=data.get('settings')):
                response = self.post_form(data)
                self.assertEqual(response.status_code, 202, response.content)
                job = GenerationJob.objects.get(pk=response.json()['job_id'])
                self.assertEqual((job.settings['width'], job.settings['frame_count']), (832, 81))

    def test_deeply_nested_json_body_is_a_400(self):
        # Used to make Python's JSON decoder raise RecursionError: a 500.
        response = self.post_json('{"prompt": ' + '[' * 100_000 + ']' * 100_000 + '}')
        error = self.assertRejected(response, 'invalid_request', fields=None)
        self.assertEqual(error['message'], 'JSON parse error - the data is nested too deeply.')


class SettingsTypeTests(FieldTestCase):
    WRONG_TYPES = [
        ('width', '832'), ('width', 832.0), ('width', 832.5), ('width', True), ('width', None), ('width', [832]),
        ('height', '480'), ('frame_count', '33'), ('frame_count', 33.0), ('fps', '16'), ('fps', False),
        ('seed', '42'), ('seed', 4.2), ('seed', True),
        ('enhance_prompt', 'true'), ('enhance_prompt', 1), ('enhance_prompt', 0), ('enhance_prompt', 'yes'),
        ('enhance_prompt', None),
        ('model_profile', 5), ('model_profile', None), ('model_profile', {'name': 'x'}),
        ('style', 5), ('style', True), ('style', ['anime']),
    ]

    def test_text_to_video_wrong_types(self):
        for name, value in self.WRONG_TYPES:
            with self.subTest(name=name, value=value):
                error = self.assertRejected(self.post_json({'prompt': 'a', 'settings': {name: value}}),
                                            'validation_error', fields=['settings'])
                self.assertEqual(list(error['fields']['settings']), [name])

    def test_image_to_video_wrong_types(self):
        for name, value in [('frame_count', '33'), ('width', 832.0), ('seed', '7'), ('style', 1)]:
            with self.subTest(name=name, value=value):
                response = self.post_form({'image': png(), 'settings': json.dumps({name: value})})
                error = self.assertRejected(response, 'validation_error', fields=['settings'])
                self.assertEqual(list(error['fields']['settings']), [name])

    def test_null_where_allowed(self):
        response = self.post_json({'prompt': 'a', 'settings': {'seed': None, 'style': None}})
        self.assertEqual(response.status_code, 202, response.content)


class ValidRequestTests(FieldTestCase):
    """The requests the frontend sends, and the documented full forms, still work."""

    def test_text_to_video_as_the_frontend_sends_it(self):
        settings = {'width': 480, 'height': 832, 'frame_count': 33, 'style': 'anime'}
        response = self.post_json({'prompt': 'A lighthouse at dusk', 'settings': settings})
        self.assertEqual(response.status_code, 202, response.content)
        job = GenerationJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual(job.settings, {**TEXT_SETTINGS, **settings})

    def test_text_to_video_with_every_setting(self):
        settings = {'model_profile': 'wan2.1-t2v-1.3b-480p', 'width': 832, 'height': 480, 'frame_count': 81,
                    'fps': 16, 'seed': 42, 'style': 'cinematic', 'enhance_prompt': True}
        response = self.post_json({'prompt': 'A cat', 'settings': settings})
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).settings, settings)

    def test_image_to_video_as_the_frontend_sends_it(self):
        settings = {'width': 832, 'height': 480, 'frame_count': 49, 'style': 'realistic'}
        response = self.post_form({'image': png(), 'prompt': 'Wave hello', 'settings': json.dumps(settings)})
        self.assertEqual(response.status_code, 202, response.content)
        job = GenerationJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual((job.mode, job.settings['frame_count'], job.settings['style']), ('image_to_video', 49, 'realistic'))
        self.assertTrue((self.media_root / job.input_file.name).exists())

    def test_image_to_video_with_the_profile_the_frontend_selects(self):
        # main.js buildSettings() adds the default profile's name once GET /profiles/ has answered.
        settings = {'model_profile': 'wan2.1-i2v-14b-480p', 'width': 480, 'height': 832, 'frame_count': 33,
                    'style': '3d_animation'}
        response = self.post_form({'image': png(), 'prompt': '', 'settings': json.dumps(settings)})
        self.assertEqual(response.status_code, 202, response.content)
        job = GenerationJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual({k: job.settings[k] for k in settings}, settings)
        self.assertEqual((job.mode, job.prompt_text), ('image_to_video', ''))

    def test_text_to_video_form_body_with_settings_text(self):
        response = self.client.post(TEXT_URL, {'prompt': 'A cat', 'settings': json.dumps({'frame_count': 17})})
        self.assertEqual(response.status_code, 202, response.content)
