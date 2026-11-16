"""G3: uploaded images are stored as '<prompt keyword>-<uuid>.<validated extension>'."""

import re
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from apps.generator.filenames import prompt_keyword, upload_filename
from apps.generator.models import GenerationJob
from apps.generator.worker import process_job

from .base import JPEG, PNG, WEBP, WEBP_LOSSLESS, TempMediaTestCase
from .test_worker import FakeClient

IMAGE_URL = '/api/v1/generations/image-to-video/'
STORED = re.compile(r'^inputs/\d{4}/\d{2}/(?P<keyword>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<suffix>[0-9a-f]{32})\.(?P<ext>png|jpg|webp)$')


class PromptKeywordTests(SimpleTestCase):
    def test_examples(self):
        cases = {
            'A white cat walking in a garden': 'white-cat',
            'Make it wave': 'wave',
            'Café à Paris': 'cafe-paris',
            'Sunset 🌅 over the sea': 'sunset-sea',
            '東京の夜景 🌃': 'image',
            '🌃🌅': 'image',
            '': 'image',
            None: 'image',
            '   \u200b\u200d  ': 'image',
            'the a an of': 'image',
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(prompt_keyword(prompt), expected)

    def test_unsafe_characters_removed(self):
        cases = {
            '../../etc/passwd': 'etc-passwd',
            'C:\\Windows\\system32\\cmd.exe': 'c-windows',
            'cat\x00\x1f\x7fdog': 'cat-dog',
            'name<script>alert(1)</script>': 'name-script',
            '"quotes" & | ; $(rm -rf /)': 'quotes-rm',
            'evil\u202etxt.exe': 'evil-txt',  # right-to-left override
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(prompt_keyword(prompt), expected)

    def test_length_is_limited(self):
        self.assertEqual(prompt_keyword('x' * 100), 'x' * 24)
        self.assertEqual(prompt_keyword('extraordinarily magnificent mountains'), 'extraordinarily')
        long_prompt = 'dragon ' * 500
        self.assertLessEqual(len(prompt_keyword(long_prompt)), 24)

    def test_filename_format_and_uniqueness(self):
        names = {upload_filename('A white cat', 'png') for _ in range(50)}
        self.assertEqual(len(names), 50)
        for name in names:
            self.assertRegex(name, r'^white-cat-[0-9a-f]{32}\.png$')
        self.assertRegex(upload_filename('', 'webp'), r'^image-[0-9a-f]{32}\.webp$')


class StoredUploadTests(TempMediaTestCase):
    def post(self, content=PNG, name='photo.png', prompt=None):
        data = {'image': SimpleUploadedFile(name, content, 'image/png')}
        if prompt is not None:
            data['prompt'] = prompt
        response = self.client.post(IMAGE_URL, data)
        self.assertEqual(response.status_code, 202, response.content[:300])
        return GenerationJob.objects.get(pk=response.json()['job_id'])

    def stored(self, job):
        match = STORED.match(job.input_file.name)
        self.assertIsNotNone(match, job.input_file.name)
        return match

    def test_keyword_suffix_and_extension(self):
        job = self.post(prompt='A white cat walking in a garden')
        match = self.stored(job)
        self.assertEqual((match['keyword'], match['ext']), ('white-cat', 'png'))
        self.assertEqual((self.media_root / job.input_file.name).read_bytes(), PNG)

    def test_extension_comes_from_content(self):
        for content, name, ext in ((JPEG, 'photo.png', 'jpg'), (WEBP, 'photo.jpg', 'webp'),
                                   (WEBP_LOSSLESS, 'x.gif', 'webp'), (PNG, 'noext', 'png')):
            with self.subTest(ext=ext, name=name):
                self.assertEqual(self.stored(self.post(content, name, 'Sunset'))['ext'], ext)

    def test_fallback_keyword(self):
        for prompt in (None, '', '   ', '東京の夜景 🌃'):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.stored(self.post(prompt=prompt))['keyword'], 'image')

    def test_identical_prompts_do_not_collide(self):
        first = self.post(prompt='A white cat')
        second = self.post(prompt='A white cat')
        self.assertNotEqual(first.input_file.name, second.input_file.name)
        self.assertNotEqual(self.stored(first)['suffix'], self.stored(second)['suffix'])
        self.assertTrue((self.media_root / first.input_file.name).is_file())
        self.assertTrue((self.media_root / second.input_file.name).is_file())

    def test_original_filename_has_no_influence(self):
        for name in ('../../evil.html', '..\\..\\evil.html', 'C:\\evil.exe', 'evil.php.png', 'x' * 300 + '.png',
                     '<script>.png', 'con.png'):
            with self.subTest(name=name):
                job = self.post(name=name, prompt='A white cat')
                self.assertEqual(self.stored(job)['keyword'], 'white-cat')
                self.assertNotIn('evil', job.input_file.name)
                path = (self.media_root / job.input_file.name).resolve()
                self.assertTrue(path.is_relative_to((self.media_root / 'inputs').resolve()))

    def test_worker_receives_the_stored_file(self):
        job = self.post(prompt='A white cat')
        job.transition_to(GenerationJob.Status.PROCESSING)
        client = FakeClient()
        process_job(job, client=client)
        self.assertEqual(client.request.image_path, Path(job.input_file.path))
        self.assertTrue(client.request.image_path.is_file())
        self.assertRegex(client.request.image_path.name, r'^white-cat-[0-9a-f]{32}\.png$')
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.COMPLETED)

    def test_existing_jobs_keep_their_files(self):
        legacy = self.make_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
        legacy.input_file.save('My Holiday Photo.png', ContentFile(PNG), save=True)
        legacy_name = legacy.input_file.name
        self.post(prompt='A white cat')
        legacy.refresh_from_db()
        self.assertEqual(legacy.input_file.name, legacy_name)
        self.assertEqual((self.media_root / legacy_name).read_bytes(), PNG)
