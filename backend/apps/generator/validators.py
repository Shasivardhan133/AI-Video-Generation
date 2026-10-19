"""Validation helpers that do not need extra libraries such as Pillow."""

from rest_framework import serializers

MAX_IMAGE_BYTES = 10 * 1024 * 1024

# File signatures ("magic bytes") for the accepted image formats.
IMAGE_SIGNATURES = {
    'png': lambda head: head.startswith(b'\x89PNG\r\n\x1a\n'),
    'jpeg': lambda head: head.startswith(b'\xff\xd8\xff'),
    'webp': lambda head: head[:4] == b'RIFF' and head[8:12] == b'WEBP',
}


def detect_image_format(upload):
    """Return 'png', 'jpeg' or 'webp' from the file contents, or None."""
    position = upload.tell()
    head = upload.read(12)
    upload.seek(position)
    for name, matches in IMAGE_SIGNATURES.items():
        if matches(head):
            return name
    return None


def validate_image_upload(upload):
    if upload.size > MAX_IMAGE_BYTES:
        raise serializers.ValidationError('Images must be 10 MB or smaller.', code='image_too_large')
    if upload.size == 0:
        raise serializers.ValidationError('The uploaded image is empty.', code='invalid_image')
    if detect_image_format(upload) is None:
        raise serializers.ValidationError(
            'Only PNG, JPEG, and WebP images are supported.',
            code='unsupported_image_type',
        )
    return upload
