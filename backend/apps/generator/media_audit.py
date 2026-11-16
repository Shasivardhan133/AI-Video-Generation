"""Read-only audit of MEDIA_ROOT: which stored files no database record refers to.

Nothing here writes, moves or deletes a file or a database row: files are only
listed and stat()-ed (never opened), and the database is only read.

Every regular file under MEDIA_ROOT is put in one category:

- `referenced`: a FileField value of some model names it.
- `orphan`: unreferenced, older than the grace period, and not the output of
  a job that is still active. A candidate for manual review, nothing more.
- `possibly_in_use`: unreferenced but either recently modified (younger than
  the grace period) or named after an active job's output. The worker saves a
  finished video before it records the reference, and an upload is written
  just before its job row is inserted, so such files are normally about to be
  referenced. They are reported, never counted as orphans.
- `ignored`: repository placeholders such as `.gitkeep`.

Also reported: database references whose file is missing, references that
point outside MEDIA_ROOT, and symbolic links or junctions, which are never
followed (so the scan cannot leave MEDIA_ROOT). A MEDIA_ROOT that is a drive
or file-system root, or that contains the application (BASE_DIR or
FRONTEND_DIR), is refused before anything is scanned (UnsafeMediaRoot).

Output never contains prompts, session keys, owners or secrets. Stored input
names begin with a keyword taken from the prompt, so that part of a file name
is masked unless names are explicitly requested.
"""

import os
import re
import stat
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import models
from django.utils import timezone

from .media_files import is_inside as _inside
from .models import GenerationJob

PLACEHOLDER_NAMES = frozenset({'.gitkeep', '.gitignore', '.keep'})
ACTIVE_STATUSES = (GenerationJob.Status.QUEUED, GenerationJob.Status.PROCESSING)
DEFAULT_GRACE_MINUTES = 60
# A stored input name: '<keyword>-<32 hex>.<ext>' (apps.generator.filenames).
INPUT_NAME = re.compile(r'^(?P<keyword>.+)-(?P<token>[0-9a-f]{32})(?P<ext>\.[A-Za-z0-9]+)$')
# A stored output name: '<job uuid>.mp4', possibly with a storage suffix ('_AbC1234').
OUTPUT_NAME = re.compile(r'^(?P<job>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:_[A-Za-z0-9]+)?\.mp4$')


@dataclass
class MediaFile:
    path: str  # relative to MEDIA_ROOT, '/'-separated, possibly masked
    category: str
    reason: str
    size: int
    modified: str  # UTC, ISO 8601
    job_id: str = ''
    job_status: str = ''


@dataclass
class MediaAudit:
    media_root: str
    grace_minutes: int
    files: list = field(default_factory=list)
    missing: list = field(default_factory=list)  # [{'model', 'field', 'job_id', 'path', 'status'}]
    outside_root: list = field(default_factory=list)
    links: list = field(default_factory=list)  # paths of skipped links/junctions

    def of(self, category):
        return [f for f in self.files if f.category == category]

    def summary(self):
        counts = {}
        for name in ('referenced', 'orphan', 'possibly_in_use', 'ignored'):
            chosen = self.of(name)
            counts[name] = {'count': len(chosen), 'bytes': sum(f.size for f in chosen)}
        return counts

    def as_dict(self):
        return {
            'media_root': self.media_root, 'grace_minutes': self.grace_minutes, 'summary': self.summary(),
            'files': [asdict(f) for f in self.files if f.category != 'referenced'],
            'missing_files': self.missing, 'references_outside_media_root': self.outside_root,
            'skipped_links': self.links,
        }


def mask_name(relative, show_names):
    """Hide the prompt keyword of a stored input name ('inputs/2026/10/*-<hex>.png')."""
    if show_names:
        return relative
    folder, _, name = relative.rpartition('/')
    match = INPUT_NAME.match(name)
    if match and relative.startswith('inputs/'):
        name = f'*-{match["token"]}{match["ext"]}'
    return f'{folder}/{name}' if folder else name


def file_field_references():
    """{relative name: (model label, field name, primary key)} for every FileField on default storage."""
    references = {}
    for model in apps.get_models():
        for model_field in model._meta.get_fields():
            if not isinstance(model_field, models.FileField) or model_field.storage is not default_storage:
                continue
            rows = (model._default_manager.exclude(**{model_field.name: ''})
                    .values_list('pk', model_field.name).iterator())
            for pk, name in rows:
                references.setdefault(name.replace('\\', '/'), (model._meta.label, model_field.name, str(pk)))
    return references


class UnsafeMediaRoot(ValueError):
    """MEDIA_ROOT is a drive or file-system root, or contains the application itself.

    Every unreferenced file there would be listed as an orphan candidate,
    including source code and configuration, so the audit refuses to run.
    """


def check_media_root(root):
    if os.path.dirname(root) == root:
        raise UnsafeMediaRoot(f'MEDIA_ROOT is the root of a drive or file system ({root}); not auditing it.')
    for name in ('BASE_DIR', 'FRONTEND_DIR'):
        app_folder = getattr(settings, name, None)
        if app_folder and _inside(root, os.path.realpath(app_folder)):
            raise UnsafeMediaRoot(f'MEDIA_ROOT ({root}) contains application files ({name}); not auditing it. '
                                  'Point MEDIA_ROOT at a folder used only for media.')


def _is_link(path):
    return os.path.islink(path) or (hasattr(os.path, 'isjunction') and os.path.isjunction(path))


def audit_media(grace_minutes=DEFAULT_GRACE_MINUTES, show_names=False, now=None):
    root = os.path.realpath(settings.MEDIA_ROOT)
    now = now or timezone.now()
    grace_cutoff = now - timedelta(minutes=grace_minutes)
    report = MediaAudit(media_root=root, grace_minutes=grace_minutes)
    if not os.path.isdir(root):
        raise FileNotFoundError(f'MEDIA_ROOT does not exist: {root}')
    check_media_root(root)

    references = file_field_references()
    statuses = dict(GenerationJob.objects.values_list('id', 'status').iterator())
    statuses = {str(job_id): status for job_id, status in statuses.items()}

    # Database references: missing files and names that would leave MEDIA_ROOT.
    for name, (label, field_name, pk) in sorted(references.items()):
        absolute = os.path.realpath(os.path.join(root, name))
        entry = {'model': label, 'field': field_name, 'job_id': pk, 'status': statuses.get(pk, ''),
                 'path': mask_name(name, show_names)}
        if os.path.isabs(name) or not _inside(root, absolute):
            report.outside_root.append(entry)
        elif not os.path.isfile(absolute):
            report.missing.append(entry)

    # Files on disk. os.walk never follows links; links and junctions are listed, not entered.
    for folder, dirnames, filenames in os.walk(root, followlinks=False):
        for dirname in list(dirnames):
            full = os.path.join(folder, dirname)
            if _is_link(full):
                dirnames.remove(dirname)
                report.links.append(Path(full).relative_to(root).as_posix())
        dirnames.sort()
        for filename in sorted(filenames):
            full = os.path.join(folder, filename)
            relative = Path(full).relative_to(root).as_posix()
            info = os.lstat(full)
            if _is_link(full) or not stat.S_ISREG(info.st_mode) or not _inside(root, os.path.realpath(full)):
                report.links.append(relative)
                continue
            modified = datetime.fromtimestamp(info.st_mtime, tz=dt_timezone.utc)
            item = MediaFile(path=mask_name(relative, show_names), category='', reason='', size=info.st_size,
                             modified=modified.isoformat(timespec='seconds'))
            output = OUTPUT_NAME.match(filename) if relative.startswith('outputs/') else None
            if output:
                item.job_id = output['job']
                item.job_status = statuses.get(item.job_id, 'deleted')
            if filename in PLACEHOLDER_NAMES:
                item.category, item.reason = 'ignored', 'repository placeholder'
            elif relative in references:
                item.category, item.reason = 'referenced', 'named by ' + '.'.join(references[relative][:2])
            elif output and item.job_status in ACTIVE_STATUSES:
                item.category, item.reason = 'possibly_in_use', f'output of a {item.job_status} job (it may be recorded soon)'
            elif modified > grace_cutoff:
                item.category, item.reason = 'possibly_in_use', f'modified within the last {grace_minutes} minutes'
            else:
                item.category, item.reason = 'orphan', _orphan_reason(relative, item)
            report.files.append(item)
    return report


def _orphan_reason(relative, item):
    if item.job_id and item.job_status == 'deleted':
        return 'output of a job that no longer exists'
    if item.job_id:
        return f'output of a {item.job_status} job that does not refer to it'
    if relative.startswith('inputs/'):
        return 'input image that no job refers to'
    if relative.startswith('outputs/'):
        return 'file in outputs/ that no job refers to'
    return 'file outside inputs/ and outputs/ that nothing refers to'
