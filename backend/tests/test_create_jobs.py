"""POST /generations/text-to-video/ and /generations/image-to-video/."""

import json
import uuid

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.generator.models import GenerationJob
from apps.generator.validators import MAX_IMAGE_BYTES

from .base import GIF, IMAGE_SETTINGS, JPEG, PNG, TEXT_SETTINGS, WEBP, TempMediaTestCase, make_png

TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'


def image(content=PNG, name='photo.png', content_type='image/png'):
    return SimpleUploadedFile(name, content, content_type=content_type)


class TextToVideoCreateTests(TempMediaTestCase):
    def post(self, data):
        return self.client.post(TEXT_URL, data, content_type='application/json')

    def test_creates_queued_job(self):
        response = self.post({'prompt': 'A lighthouse at dusk'})
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(set(body), {'job_id', 'mode', 'status', 'created_at', 'status_url'})
        self.assertEqual(body['mode'], 'text_to_video')
        self.assertEqual(body['status'], 'queued')
        self.assertEqual(body['status_url'], f"/api/v1/generations/{body['job_id']}/")

        job = GenerationJob.objects.get(pk=uuid.UUID(body['job_id']))
        self.assertEqual(job.status, GenerationJob.Status.QUEUED)
        self.assertEqual(job.mode, GenerationJob.Mode.TEXT_TO_VIDEO)
        self.assertEqual(job.prompt_text, 'A lighthouse at dusk')
        self.assertEqual(job.settings, TEXT_SETTINGS)
        self.assertIsNone(job.user)
        self.assertEqual(job.session_key, self.client.session.session_key)
        self.assertEqual(job.request_id, response['X-Request-ID'])
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.completed_at)
        self.assertFalse(job.input_file)
        self.assertFalse(job.output_file)

    def test_signed_in_user_owns_job(self):
        user = get_user_model().objects.create_user('alice', password='unused-test-password')
        self.client.force_login(user)
        response = self.post({'prompt': 'A cat'})
        job = GenerationJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual(job.user, user)
        self.assertEqual(job.session_key, '')

    def test_prompt_is_trimmed(self):
        response = self.post({'prompt': '   A cat   '})
        self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).prompt_text, 'A cat')

    def test_prompt_length_limit(self):
        self.assertEqual(self.post({'prompt': 'x' * 1000}).status_code, 202)
        self.assertApiError(self.post({'prompt': 'x' * 1001}), 400, 'validation_error', fields=['prompt'])

    def test_missing_blank_and_whitespace_prompts_rejected(self):
        for data in [{}, {'prompt': ''}, {'prompt': '    '}, {'prompt': None}]:
            with self.subTest(data=data):
                self.assertApiError(self.post(data), 400, 'validation_error', fields=['prompt'])
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_custom_settings_are_stored(self):
        settings = {'width': 480, 'height': 832, 'frame_count': 33, 'seed': 42,
                    'style': 'anime', 'enhance_prompt': True}
        response = self.post({'prompt': 'A cat', 'settings': settings})
        self.assertEqual(response.status_code, 202)
        job = GenerationJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual(job.settings, {**TEXT_SETTINGS, **settings})

    def test_seed_bounds(self):
        for seed in (0, 2**31 - 1):
            self.assertEqual(self.post({'prompt': 'a', 'settings': {'seed': seed}}).status_code, 202)
        for seed in (-1, 2**31):
            with self.subTest(seed=seed):
                self.assertApiError(self.post({'prompt': 'a', 'settings': {'seed': seed}}),
                                    400, 'validation_error', fields=['settings'])

    def test_unsupported_settings_rejected(self):
        cases = [
            {'width': 1280, 'height': 720},
            {'width': 480},  # 480x480 is not an allowed size
            {'frame_count': 50},  # not 4n + 1
            {'frame_count': 13},  # below minimum
            {'frame_count': 85},  # above maximum
            {'fps': 30},
            {'model_profile': 'wan2.1-i2v-14b-480p'},  # image profile in text mode
            {'model_profile': 'does-not-exist'},
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                self.assertApiError(self.post({'prompt': 'a', 'settings': settings}),
                                    400, 'unsupported_setting', fields=['settings'])
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_invalid_settings_values_rejected(self):
        cases = [{'style': 'watercolour'}, {'width': 'wide'}, {'seed': 'abc'}]
        for settings in cases:
            with self.subTest(settings=settings):
                self.assertApiError(self.post({'prompt': 'a', 'settings': settings}),
                                    400, 'validation_error', fields=['settings'])

    def test_settings_must_be_an_object(self):
        for settings in (['a'], 5, 'not json'):
            with self.subTest(settings=settings):
                self.assertApiError(self.post({'prompt': 'a', 'settings': settings}),
                                    400, 'validation_error', fields=['settings'])

    def test_form_encoded_request_with_json_settings_string(self):
        response = self.client.post(TEXT_URL, {'prompt': 'A cat', 'settings': json.dumps({'frame_count': 17})})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).settings['frame_count'], 17)

    def test_error_fields_mirror_nested_settings(self):
        error = self.assertApiError(self.post({'prompt': 'a', 'settings': {'fps': 30}}),
                                    400, 'unsupported_setting', fields=['settings'])
        self.assertIn('fps', error['fields']['settings'])
        self.assertEqual(error['message'], 'This profile only supports 16 fps.')


class ImageToVideoCreateTests(TempMediaTestCase):
    def post(self, **data):
        return self.client.post(IMAGE_URL, data)

    def test_creates_queued_job_and_stores_image_in_media(self):
        response = self.post(image=image(), prompt='Make it wave')
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body['mode'], 'image_to_video')
        self.assertEqual(body['status'], 'queued')

        job = GenerationJob.objects.get(pk=body['job_id'])
        self.assertEqual(job.prompt_text, 'Make it wave')
        self.assertEqual(job.settings, IMAGE_SETTINGS)
        self.assertTrue(job.input_file.name.startswith('inputs/'))
        stored = self.media_root / job.input_file.name
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes(), PNG)

    def test_prompt_is_optional(self):
        for data in ({}, {'prompt': ''}):
            with self.subTest(data=data):
                response = self.post(image=image(), **data)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).prompt_text, '')

    def test_jpeg_and_webp_accepted(self):
        self.assertEqual(self.post(image=image(JPEG, 'a.jpg', 'image/jpeg')).status_code, 202)
        self.assertEqual(self.post(image=image(WEBP, 'a.webp', 'image/webp')).status_code, 202)

    def test_type_is_detected_from_content_not_name(self):
        # A real PNG with a misleading name and content type is still accepted.
        self.assertEqual(self.post(image=image(PNG, 'notes.txt', 'text/plain')).status_code, 202)

    def test_missing_image_rejected(self):
        self.assertApiError(self.post(prompt='a'), 400, 'validation_error', fields=['image'])

    def test_image_sent_as_text_rejected(self):
        self.assertApiError(self.post(image='not-a-file'), 400, 'validation_error', fields=['image'])

    def test_unsupported_image_types_rejected(self):
        cases = {
            'gif': image(GIF, 'a.gif', 'image/gif'),
            'gif renamed to png': image(GIF, 'a.png', 'image/png'),
            'text renamed to png': image(b'hello world, not an image', 'a.png', 'image/png'),
            'truncated png signature': image(PNG[:4], 'a.png', 'image/png'),
            'riff that is not webp': image(b'RIFF\x00\x00\x00\x00WAVEfmt ' + b'\x00' * 32, 'a.webp', 'image/webp'),
        }
        for label, upload in cases.items():
            with self.subTest(label):
                self.assertApiError(self.post(image=upload), 400, 'unsupported_image_type', fields=['image'])
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_size_limit(self):
        # A real 25 MB (MiB) request body: the image limit fits inside the 26 MB request limit.
        self.assertEqual(MAX_IMAGE_BYTES, 25 * 1024 * 1024)
        response = self.post(image=image(make_png(total_size=MAX_IMAGE_BYTES)))
        self.assertEqual(response.status_code, 202, response.content[:300])
        self.assertApiError(self.post(image=image(make_png(total_size=MAX_IMAGE_BYTES + 1))),
                            400, 'image_too_large', fields=['image'])

    def test_empty_image_rejected_with_documented_code(self):
        error = self.assertApiError(self.post(image=image(b''), prompt='Make it wave'),
                                    400, 'invalid_image', fields=['image'])
        self.assertEqual(error['message'], 'The uploaded image is empty.')
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertFalse((self.media_root / 'inputs').exists())

    def test_rejected_upload_is_not_stored(self):
        self.post(image=image(GIF, 'a.gif', 'image/gif'))
        self.assertFalse((self.media_root / 'inputs').exists())

    def test_prompt_enhancement_not_available_for_image_profile(self):
        response = self.post(image=image(), settings=json.dumps({'enhance_prompt': True}))
        self.assertApiError(response, 400, 'unsupported_setting', fields=['settings'])

    def test_text_profile_rejected_in_image_mode(self):
        response = self.post(image=image(), settings=json.dumps({'model_profile': 'wan2.1-t2v-1.3b-480p'}))
        self.assertApiError(response, 400, 'unsupported_setting', fields=['settings'])

    def test_invalid_settings_json_rejected(self):
        self.assertApiError(self.post(image=image(), settings='{bad json'), 400, 'validation_error', fields=['settings'])

    def test_json_body_not_accepted(self):
        response = self.client.post(IMAGE_URL, {'prompt': 'a'}, content_type='application/json')
        self.assertApiError(response, 415, 'unsupported_media_type')
