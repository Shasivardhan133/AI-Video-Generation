"""Upload validation that does not need extra libraries such as Pillow.

Images are checked from their contents: the format signature, the header
structure, and the dimensions it declares. Pixel data is never decoded, so a
file whose headers are valid but whose image data is corrupt can still pass;
the AI service must handle such input safely.

Units: 1 MB = 1,048,576 bytes (MiB); 1 megapixel = 1,000,000 pixels.
"""

import struct
import zlib
from dataclasses import dataclass

from rest_framework import serializers

MB = 1024 * 1024
MAX_IMAGE_BYTES = 25 * MB
MAX_IMAGE_SIDE = 4096
MAX_IMAGE_PIXELS = 16_000_000

# File signatures ("magic bytes") for the accepted image formats.
IMAGE_SIGNATURES = {
    'png': lambda head: head.startswith(b'\x89PNG\r\n\x1a\n'),
    'jpeg': lambda head: head.startswith(b'\xff\xd8\xff'),
    'webp': lambda head: head[:4] == b'RIFF' and head[8:12] == b'WEBP',
}
EXTENSIONS = {'png': 'png', 'jpeg': 'jpg', 'webp': 'webp'}


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int
    height: int

    @property
    def extension(self):
        return EXTENSIONS[self.format]


class _Malformed(Exception):
    """The header is truncated or not structured as the format requires."""


def _read(upload, offset, size):
    upload.seek(offset)
    data = upload.read(size)
    if len(data) != size:
        raise _Malformed
    return data


# --- PNG: IHDR must be the first chunk; the file must end with IEND. ---------

PNG_IEND = b'\x00\x00\x00\x00IEND\xaeB`\x82'
PNG_BIT_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}


def _png_size(upload, size):
    chunk = _read(upload, 8, 25)  # length, type, 13 data bytes, CRC
    length, chunk_type = struct.unpack('>I4s', chunk[:8])
    data, (crc,) = chunk[8:21], struct.unpack('>I', chunk[21:])
    if length != 13 or chunk_type != b'IHDR' or zlib.crc32(chunk_type + data) != crc:
        raise _Malformed
    width, height, depth, color, compression, filter_method, interlace = struct.unpack('>IIBBBBB', data)
    if depth not in PNG_BIT_DEPTHS.get(color, ()) or compression or filter_method or interlace > 1:
        raise _Malformed
    if size < 8 + 25 + len(PNG_IEND) or _read(upload, size - len(PNG_IEND), len(PNG_IEND)) != PNG_IEND:
        raise _Malformed  # truncated
    return width, height


# --- JPEG: walk the marker segments up to the frame header (SOFn). -----------

JPEG_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
JPEG_MAX_SEGMENTS = 512
JPEG_MAX_FILL_BYTES = 64


def _jpeg_size(upload, size):
    offset = 2  # after SOI
    for _ in range(JPEG_MAX_SEGMENTS):
        if _read(upload, offset, 1) != b'\xff':
            raise _Malformed
        offset += 1
        for _ in range(JPEG_MAX_FILL_BYTES):
            marker = _read(upload, offset, 1)[0]
            offset += 1
            if marker != 0xFF:
                break
        else:
            raise _Malformed
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # markers without a length field
        if marker in (0xD8, 0xD9, 0xDA):
            raise _Malformed  # SOI, EOI or start of scan before any frame header
        (length,) = struct.unpack('>H', _read(upload, offset, 2))
        if length < 2:
            raise _Malformed
        if marker in JPEG_SOF_MARKERS:
            if length < 8:
                raise _Malformed
            precision, height, width, components = struct.unpack('>BHHB', _read(upload, offset + 2, 6))
            if precision not in (8, 12, 16) or not 1 <= components <= 4:
                raise _Malformed
            return width, height
        offset += length
    raise _Malformed


# --- WebP: RIFF container with a VP8 (lossy), VP8L (lossless) or VP8X chunk. --

def _webp_size(upload, size):
    riff_size, = struct.unpack('<I', _read(upload, 4, 4))
    if riff_size < 4 + 8 or size < riff_size + 8:
        raise _Malformed  # truncated
    fourcc, chunk_size = struct.unpack('<4sI', _read(upload, 12, 8))
    if 12 + 8 + chunk_size > riff_size + 8:
        raise _Malformed
    if fourcc == b'VP8 ':
        if chunk_size < 10:
            raise _Malformed
        data = _read(upload, 20, 10)
        if data[0] & 1 or data[3:6] != b'\x9d\x01\x2a':
            raise _Malformed  # not a key frame / missing start code
        width, height = struct.unpack('<HH', data[6:10])
        return width & 0x3FFF, height & 0x3FFF
    if fourcc == b'VP8L':
        if chunk_size < 5:
            raise _Malformed
        data = _read(upload, 20, 5)
        bits, = struct.unpack('<I', data[1:5])
        if data[0] != 0x2F or bits >> 29:
            raise _Malformed  # bad signature / unknown version
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if fourcc == b'VP8X':
        if chunk_size < 10:
            raise _Malformed
        data = _read(upload, 20, 10)
        return 1 + int.from_bytes(data[4:7], 'little'), 1 + int.from_bytes(data[7:10], 'little')
    raise _Malformed


HEADER_PARSERS = {'png': _png_size, 'jpeg': _jpeg_size, 'webp': _webp_size}


def inspect_image(upload):
    """Validate an uploaded image and return its ImageInfo, or raise ValidationError."""
    if upload.size > MAX_IMAGE_BYTES:
        raise serializers.ValidationError('Images must be 25 MB or smaller.', code='image_too_large')
    if upload.size == 0:
        raise serializers.ValidationError('The uploaded image is empty.', code='invalid_image')

    position = upload.tell()
    try:
        upload.seek(0)
        head = upload.read(12)
        image_format = next((name for name, matches in IMAGE_SIGNATURES.items() if matches(head)), None)
        if image_format is None:
            raise serializers.ValidationError(
                'Only PNG, JPEG, and WebP images are supported.', code='unsupported_image_type',
            )
        try:
            width, height = HEADER_PARSERS[image_format](upload, upload.size)
        except (_Malformed, struct.error):
            width = height = 0
    finally:
        upload.seek(position)

    if not width or not height:
        raise serializers.ValidationError('The image file is damaged or incomplete.', code='invalid_image')
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise serializers.ValidationError(
            f'Images must be at most {MAX_IMAGE_SIDE} pixels wide and {MAX_IMAGE_SIDE} pixels high.',
            code='image_dimensions_too_large',
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise serializers.ValidationError(
            'Images must be at most 16 megapixels (width x height).', code='image_dimensions_too_large',
        )
    return ImageInfo(image_format, width, height)


def validate_image_upload(upload):
    inspect_image(upload)
    return upload
