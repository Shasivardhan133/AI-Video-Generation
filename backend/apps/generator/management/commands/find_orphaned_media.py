import json

from django.core.management.base import BaseCommand, CommandError

from apps.generator.media_audit import DEFAULT_GRACE_MINUTES, UnsafeMediaRoot, audit_media


def human(size):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


class Command(BaseCommand):
    help = ('Report media files that no database record refers to. REPORT ONLY: it never modifies, moves or '
            'deletes a file or a database record, and has no option to do so. Files that may still be in use '
            '(recently modified, or the output of a queued/processing job) are listed separately from orphan '
            'candidates. Prompt keywords in input file names are masked unless --show-file-names is given.')

    def add_arguments(self, parser):
        parser.add_argument('--grace-minutes', type=int, default=DEFAULT_GRACE_MINUTES,
                            help=f'Unreferenced files modified more recently than this are reported as possibly '
                                 f'in use, not as orphans (default {DEFAULT_GRACE_MINUTES}).')
        parser.add_argument('--json', action='store_true', help='Print the report as JSON.')
        parser.add_argument('--show-file-names', action='store_true',
                            help='Print input file names in full; they contain a keyword from the prompt.')

    def handle(self, *args, **options):
        if options['grace_minutes'] < 0:
            raise CommandError('--grace-minutes must be 0 or more.')
        try:
            report = audit_media(grace_minutes=options['grace_minutes'], show_names=options['show_file_names'])
        except (FileNotFoundError, UnsafeMediaRoot) as exc:
            raise CommandError(str(exc)) from None
        if options['json']:
            self.stdout.write(json.dumps(report.as_dict(), indent=2))
            return
        self.write_text(report)

    def write_text(self, report):
        out = self.stdout.write
        summary = report.summary()
        out(f'Media audit of {report.media_root} (report only: nothing was changed)')
        out(f'Grace period: {report.grace_minutes} minutes')
        out('')
        labels = (('referenced', 'Referenced by the database'), ('orphan', 'Orphan candidates'),
                  ('possibly_in_use', 'Possibly in use (not orphans)'), ('ignored', 'Ignored placeholders'))
        for key, label in labels:
            out(f'  {label + ":":38} {summary[key]["count"]:5} files  {human(summary[key]["bytes"]):>10}')
        out(f'  {"Database references to missing files:":38} {len(report.missing):5}')
        out(f'  {"References outside the media root:":38} {len(report.outside_root):5}')
        out(f'  {"Links and junctions (not followed):":38} {len(report.links):5}')
        for key, title in (('orphan', 'Orphan candidates (review manually before removing anything)'),
                           ('possibly_in_use', 'Possibly in use (leave alone; re-run the audit later)')):
            files = report.of(key)
            if files:
                out('')
                out(f'{title}:')
                for item in files:
                    job = f'  job {item.job_id} ({item.job_status})' if item.job_id else ''
                    out(f'  {item.path}  {human(item.size)}  modified {item.modified}  - {item.reason}{job}')
        for entries, title in ((report.missing, 'Database references to missing files'),
                               (report.outside_root, 'References outside the media root (not checked on disk)')):
            if entries:
                out('')
                out(f'{title}:')
                for entry in entries:
                    out(f'  {entry["path"]}  {entry["model"]}.{entry["field"]}  job {entry["job_id"]} ({entry["status"]})')
        if report.links:
            out('')
            out('Links and junctions (not followed, not classified):')
            for path in report.links:
                out(f'  {path}')
