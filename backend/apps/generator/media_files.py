"""Stored media files: where a database reference really leads, and writing new ones.

FileField values are names relative to MEDIA_ROOT. Before a stored file is
served or sent to the AI service, stored_file_path() resolves its reference;
the file is used only if the result is a regular file inside MEDIA_ROOT.
Traversal ('../x'), absolute paths, directories, and symlinks or junctions
whose target lies outside MEDIA_ROOT all count as missing. Links that stay
inside MEDIA_ROOT, or a MEDIA_ROOT that is itself reached through a link, work
as usual: both sides of the comparison are resolved.

store_new_file() writes a new upload or video under a name no file has, so
nothing is ever overwritten, and removes the partly written file if the write
fails (FileSystemStorage leaves it behind, for example when the disk is full).
"""

import logging
import os
import re
from pathlib import Path

from django.core.exceptions import SuspiciousFileOperation

logger = logging.getLogger(__name__)


def is_inside(root, path):
    """True if `path` is `root` or below it (both already resolved)."""
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # different drives on Windows
        return False


def stored_file_path(field_file):
    """The resolved path of a FieldFile's stored file, or None if it must not be used.

    The file could still be replaced between this check and the moment it is
    opened, but only by someone who can already write to MEDIA_ROOT.
    """
    if not field_file:
        return None
    storage = field_file.storage
    try:
        root = os.path.realpath(storage.location)
        real = os.path.realpath(storage.path(field_file.name))  # SuspiciousFileOperation for '../x' or 'C:/x'
    except (SuspiciousFileOperation, NotImplementedError, ValueError, OSError):
        return None
    if not is_inside(root, real) or not os.path.isfile(real):
        return None
    return Path(real)


def store_new_file(field, instance, filename, content):
    """Save `content` for a FileField under a new, unused name and return the name.

    If the write fails part-way, whatever it left at that name is deleted. It
    can only be this write's own file: the name was unused when it was chosen
    and contains a value that no other operation writes at the same time (a
    fresh random UUID for an upload; for a video, the job's UUID, which only the
    worker holding that job's lease writes).
    """
    storage = field.storage
    name = storage.get_available_name(field.generate_filename(instance, filename), max_length=field.max_length)
    try:
        return storage.save(name, content, max_length=field.max_length)
    except BaseException:
        discard_file(storage, name, 'partly written file')
        raise


def loggable_name(name):
    """A stored name for the logs, with the prompt keyword of an upload name masked
    ('inputs/2026/10/white-cat-<hex>.png' -> 'inputs/2026/10/*-<hex>.png'), as the media audit does."""
    return PROMPT_KEYWORD.sub(r'\1*-', str(name).replace('\\', '/'))


PROMPT_KEYWORD = re.compile(r'(^|/)[^/]+-(?=[0-9a-f]{32}\.[A-Za-z0-9]+$)')


def discard_file(storage, name, description):
    """Delete a file this operation created and no record refers to; never raises OSError."""
    try:
        storage.delete(name)
    except OSError as exc:
        logger.error('Could not delete %s %s (%s)', description, loggable_name(name), type(exc).__name__)
        return False
    logger.info('Deleted %s %s', description, loggable_name(name))
    return True
