"""G2: image validation from file contents (signature, header structure, dimensions).

Pixel data is never decoded; these tests cover what the headers can prove.
"""

import struct
import zlib

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from apps.generator.models import GenerationJob
from apps.generator.validators import MAX_IMAGE_BYTES, ImageInfo, inspect_image

from .base import GIF, JPEG, PNG, WEBP, WEBP_EXTENDED, WEBP_LOSSLESS, TempMediaTestCase, make_png

IMAGE_URL = '/api/v1/generations/image-to-video/'


def upload(content, name='upload.bin'):
    return SimpleUploadedFile(name, content, content_type='application/octet-stream')


def png_with_ihdr(width=2, height=3, depth=8, color=2, crc_ok=True, length=13):
    data = struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, 0)
    crc = zlib.crc32(b'IHDR' + data) ^ (0 if crc_ok else 1)
    return PNG[:8] + struct.pack('>I', length) + b'IHDR' + data + struct.pack('>I', crc) + PNG[33:]


def jpeg_with_frame(width, height, marker=0xC0, precision=8, components=3):
    sof = struct.pack('>BHHB', precision, height, width, components) + b'\x01\x22\x00' * components
    return b'\xff\xd8' + b'\xff' + bytes([marker]) + struct.pack('>H', 2 + len(sof)) + sof + b'\xff\xd9'


def webp_vp8x(width, height):
    data = bytearray(WEBP_EXTENDED)
    data[24:27] = (width - 1).to_bytes(3, 'little')
    data[27:30] = (height - 1).to_bytes(3, 'little')
    return bytes(data)


def webp_vp8l(width, height):
    data = bytearray(WEBP_LOSSLESS)
    bits = struct.unpack('<I', data[21:25])[0] & ~((1 << 28) - 1)
    data[21:25] = struct.pack('<I', bits | (width - 1) | ((height - 1) << 14))
    return bytes(data)


def webp_vp8(width, height):
    data = bytearray(WEBP)
    data[26:30] = struct.pack('<HH', width, height)
    return bytes(data)


class InspectImageTests(SimpleTestCase):
    def assertRejected(self, content, code, size=None):
        file = upload(content)
        if size is not None:
            file.size = size
        with self.assertRaises(ValidationError) as ctx:
            inspect_image(file)
        self.assertEqual(ctx.exception.detail[0].code, code, ctx.exception.detail)
        return str(ctx.exception.detail[0])

    def test_valid_images_and_extensions(self):
        cases = {
            'png': (PNG, ImageInfo('png', 2, 3), 'png'),
            'jpeg': (JPEG, ImageInfo('jpeg', 2, 3), 'jpg'),
            'webp lossy (VP8)': (WEBP, ImageInfo('webp', 2, 3), 'webp'),
            'webp lossless (VP8L)': (WEBP_LOSSLESS, ImageInfo('webp', 2, 3), 'webp'),
            'webp extended (VP8X)': (WEBP_EXTENDED, ImageInfo('webp', 2, 3), 'webp'),
        }
        for label, (content, expected, extension) in cases.items():
            with self.subTest(label):
                info = inspect_image(upload(content))
                self.assertEqual(info, expected)
                self.assertEqual(info.extension, extension)

    def test_content_decides_not_name(self):
        self.assertEqual(inspect_image(upload(PNG, 'photo.jpg')).format, 'png')
        self.assertEqual(self.assertRejected(GIF, 'unsupported_image_type'), 'Only PNG, JPEG, and WebP images are supported.')

    def test_file_position_is_restored(self):
        file = upload(JPEG)
        file.seek(5)
        inspect_image(file)
        self.assertEqual(file.tell(), 5)

    def test_empty_and_unsupported(self):
        self.assertRejected(b'', 'invalid_image')
        for content in (GIF, b'BM' + b'\x00' * 60, b'hello world', b'\x00' * 64, PNG[:4],
                        b'RIFF\x00\x00\x00\x00WAVEfmt ' + b'\x00' * 32):
            with self.subTest(content=content[:12]):
                self.assertRejected(content, 'unsupported_image_type')

    def test_size_limit_25_mb(self):
        self.assertEqual(MAX_IMAGE_BYTES, 25 * 1024 * 1024)
        message = self.assertRejected(PNG, 'image_too_large', size=MAX_IMAGE_BYTES + 1)
        self.assertEqual(message, 'Images must be 25 MB or smaller.')
        # A real file of exactly 25 MB is accepted (also tested through the API in test_create_jobs).
        self.assertEqual(inspect_image(upload(make_png(total_size=MAX_IMAGE_BYTES))).width, 2)

    def test_malformed_png(self):
        cases = {
            'signature only': PNG[:8],
            'cut inside IHDR': PNG[:20],
            'bad IHDR checksum': png_with_ihdr(crc_ok=False),
            'wrong IHDR length': png_with_ihdr(length=12),
            'invalid bit depth': png_with_ihdr(depth=3),
            'invalid colour type': png_with_ihdr(color=5),
            'zero width': png_with_ihdr(width=0),
            'missing IEND (truncated)': PNG[:-12],
            'cut in the middle': PNG[:60],
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.assertEqual(self.assertRejected(content, 'invalid_image'), 'The image file is damaged or incomplete.')

    def test_malformed_jpeg(self):
        cases = {
            'SOI only': b'\xff\xd8\xff',
            'cut in the headers': JPEG[:30],
            'scan before frame header': b'\xff\xd8\xff\xda\x00\x02\x00\x00',
            'end of image before frame header': b'\xff\xd8\xff\xd9',
            'segment length below 2': b'\xff\xd8\xff\xe0\x00\x01' + b'\x00' * 20,
            'garbage instead of a marker': b'\xff\xd8\xff\xe0\x00\x04\x00\x00\x12\x34',
            'endless fill bytes': b'\xff\xd8' + b'\xff' * 200,
            'too many segments': b'\xff\xd8' + b'\xff\xfe\x00\x02' * 600,
            'zero height': jpeg_with_frame(2, 0),
            'bad precision': jpeg_with_frame(2, 3, precision=7),
            'bad component count': jpeg_with_frame(2, 3, components=0),
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.assertRejected(content, 'invalid_image')

    def test_jpeg_frame_variants(self):
        self.assertEqual(inspect_image(upload(jpeg_with_frame(2, 3))), ImageInfo('jpeg', 2, 3))
        self.assertEqual(inspect_image(upload(jpeg_with_frame(640, 480, marker=0xC2))), ImageInfo('jpeg', 640, 480))  # progressive
        self.assertEqual(inspect_image(upload(jpeg_with_frame(30, 20, components=1))), ImageInfo('jpeg', 30, 20))  # greyscale

    def test_malformed_webp(self):
        unknown_chunk = bytearray(WEBP)
        unknown_chunk[12:16] = b'ABCD'
        no_start_code = bytearray(WEBP)
        no_start_code[23:26] = b'\x00\x00\x00'
        not_key_frame = bytearray(WEBP)
        not_key_frame[20] |= 1
        bad_lossless_signature = bytearray(WEBP_LOSSLESS)
        bad_lossless_signature[20] = 0x00
        cases = {
            'truncated (RIFF size larger than file)': WEBP[:-5],
            'header only': WEBP[:16],
            'unknown chunk': bytes(unknown_chunk),
            'VP8 without start code': bytes(no_start_code),
            'VP8 not a key frame': bytes(not_key_frame),
            'VP8L bad signature': bytes(bad_lossless_signature),
            'VP8X cut short': WEBP_EXTENDED[:26],
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.assertRejected(content, 'invalid_image')

    def test_side_limit_4096(self):
        for build in (make_png, jpeg_with_frame, webp_vp8x, webp_vp8l, webp_vp8):
            with self.subTest(build.__name__):
                self.assertEqual(inspect_image(upload(build(4096, 1))).width, 4096)
                self.assertEqual(inspect_image(upload(build(1, 4096))).height, 4096)
                message = self.assertRejected(build(4097, 1), 'image_dimensions_too_large')
                self.assertEqual(message, 'Images must be at most 4096 pixels wide and 4096 pixels high.')
                self.assertRejected(build(1, 4097), 'image_dimensions_too_large')

    def test_pixel_limit_16_megapixels(self):
        # 16 megapixels = 16,000,000 pixels, so 4096 x 4096 (16,777,216) is too many.
        for build in (make_png, jpeg_with_frame, webp_vp8x, webp_vp8l):
            with self.subTest(build.__name__):
                self.assertEqual(inspect_image(upload(build(4000, 4000))).width, 4000)  # exactly 16,000,000
                self.assertEqual(inspect_image(upload(build(4096, 3906))).height, 3906)  # 15,998,976
                message = self.assertRejected(build(4096, 3907), 'image_dimensions_too_large')  # 16,003,072
                self.assertEqual(message, 'Images must be at most 16 megapixels (width x height).')
                self.assertRejected(build(4001, 4000), 'image_dimensions_too_large')
                self.assertRejected(build(4096, 4096), 'image_dimensions_too_large')

    def test_huge_declared_dimensions_in_a_tiny_file(self):
        # A "decompression bomb" header is rejected without decoding anything.
        content = make_png(60000, 60000)
        self.assertLess(len(content), 100)
        self.assertRejected(content, 'image_dimensions_too_large')
        self.assertRejected(webp_vp8x(16_000_000, 16_000_000), 'image_dimensions_too_large')


class ImageValidationApiTests(TempMediaTestCase):
    def post(self, content, name='photo.png'):
        return self.client.post(IMAGE_URL, {'image': SimpleUploadedFile(name, content, 'image/png'), 'prompt': 'Wave'})

    def test_every_supported_variant_creates_a_job(self):
        for content in (PNG, JPEG, WEBP, WEBP_LOSSLESS, WEBP_EXTENDED):
            self.assertEqual(self.post(content).status_code, 202)
        self.assertEqual(GenerationJob.objects.count(), 5)

    def test_rejections_through_the_api(self):
        self.assertApiError(self.post(PNG[:-12]), 400, 'invalid_image', fields=['image'])
        self.assertApiError(self.post(make_png(4097, 10)), 400, 'image_dimensions_too_large', fields=['image'])
        self.assertApiError(self.post(make_png(4096, 4096)), 400, 'image_dimensions_too_large', fields=['image'])
        self.assertApiError(self.post(GIF), 400, 'unsupported_image_type', fields=['image'])
        self.assertEqual(GenerationJob.objects.count(), 0)
        self.assertFalse((self.media_root / 'inputs').exists())
