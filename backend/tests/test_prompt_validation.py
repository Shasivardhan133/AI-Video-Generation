"""G6: strict prompt validation that keeps normal creative prompts working."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from apps.generator.models import GenerationJob
from apps.generator.serializers import ImageToVideoSerializer, has_visible_text

from .base import PNG, TempMediaTestCase

TEXT_URL = '/api/v1/generations/text-to-video/'
IMAGE_URL = '/api/v1/generations/image-to-video/'

INVISIBLE_ONLY = {
    'zero-width space': '​',
    'zero-width joiners': '​‌‍',
    'byte-order mark': '﻿',
    'word joiner': '⁠',
    'soft hyphen': '­',
    'Hangul filler': 'ㅤ',
    'Hangul choseong filler': 'ᅟ',
    'Braille blank': '⠀',
    'lone combining accent': '́',
    'variation selector': '️',
    'direction marks': '‎‏‮',
    'mixed with spaces': '  ​ ㅤ  ',
}
VISIBLE = {
    'Japanese and emoji': '東京の夜景 🌃',
    'Spanish': 'Ñandú corriendo por la pampa',
    'Arabic': 'مرحبا بالعالم',
    'Hindi': 'सूर्यास्त के समय समुद्र',
    'emoji only': '🌃',
    'emoji ZWJ sequence': '👩‍🚀 astronaut',
    'accented letters': 'Café crème',
    'digits only': '2026',
}


class HasVisibleTextTests(SimpleTestCase):
    def test_invisible_and_visible(self):
        for label, text in INVISIBLE_ONLY.items():
            with self.subTest(label):
                self.assertFalse(has_visible_text(text))
        for label, text in VISIBLE.items():
            with self.subTest(label):
                self.assertTrue(has_visible_text(text))


class TextPromptTests(TempMediaTestCase):
    def post(self, prompt):
        return self.client.post(TEXT_URL, {'prompt': prompt}, content_type='application/json')

    def test_non_string_values_rejected(self):
        for value in (123, 12.5, 0, True, False, [], ['a cat'], {}, {'text': 'a cat'}):
            with self.subTest(value=value):
                error = self.assertApiError(self.post(value), 400, 'validation_error', fields=['prompt'])
                self.assertEqual(error['fields']['prompt'], ['Not a valid string.'])
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_empty_and_whitespace_rejected(self):
        for value in ('', '   ', '\t\n\r ', ' 　'):
            with self.subTest(value=value):
                self.assertApiError(self.post(value), 400, 'validation_error', fields=['prompt'])

    def test_invisible_only_rejected(self):
        for label, value in INVISIBLE_ONLY.items():
            with self.subTest(label):
                error = self.assertApiError(self.post(value), 400, 'validation_error', fields=['prompt'])
                self.assertEqual(error['message'], 'Enter a prompt with visible text.')
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_unicode_and_emoji_accepted_unchanged(self):
        for label, value in VISIBLE.items():
            with self.subTest(label):
                response = self.post(f'  {value}  ')
                self.assertEqual(response.status_code, 202, response.content[:200])
                self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).prompt_text, value)

    def test_length_boundary(self):
        self.assertEqual(self.post('x' * 1000).status_code, 202)
        self.assertEqual(self.post('🌃' * 1000).status_code, 202)  # counted in characters
        self.assertEqual(self.post('   ' + 'x' * 1000 + '   ').status_code, 202)  # trimmed first
        self.assertApiError(self.post('x' * 1001), 400, 'validation_error', fields=['prompt'])

    def test_prompt_alone_is_enough(self):
        response = self.post('A lighthouse at dusk')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).mode, 'text_to_video')


class ImagePromptTests(TempMediaTestCase):
    def post(self, **data):
        return self.client.post(IMAGE_URL, {'image': SimpleUploadedFile('p.png', PNG, 'image/png'), **data})

    def test_prompt_optional(self):
        for data in ({}, {'prompt': ''}, {'prompt': '   '}):
            with self.subTest(data=data):
                response = self.post(**data)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).prompt_text, '')

    def test_invisible_only_prompt_rejected(self):
        for label, value in INVISIBLE_ONLY.items():
            if not value.strip():
                continue  # whitespace-only is an empty optional prompt
            with self.subTest(label):
                self.assertApiError(self.post(prompt=value), 400, 'validation_error', fields=['prompt'])
        self.assertEqual(GenerationJob.objects.count(), 0)

    def test_unicode_prompt_accepted(self):
        response = self.post(prompt='東京の夜景 🌃')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(GenerationJob.objects.get(pk=response.json()['job_id']).prompt_text, '東京の夜景 🌃')

    def test_non_string_prompt_rejected(self):
        # Multipart form values are always strings, so check the serializer directly.
        for value in (5, True, ['a'], {'a': 1}):
            with self.subTest(value=value):
                serializer = ImageToVideoSerializer(data={
                    'image': SimpleUploadedFile('p.png', PNG, 'image/png'), 'prompt': value,
                })
                self.assertFalse(serializer.is_valid())
                self.assertEqual(serializer.errors['prompt'][0].code, 'invalid')

    def test_image_still_required(self):
        self.assertApiError(self.client.post(IMAGE_URL, {'prompt': 'Wave'}), 400, 'validation_error', fields=['image'])
