import math

from django.core.management.base import BaseCommand, CommandError

from apps.generator.worker import run_forever


class Command(BaseCommand):
    help = 'Process queued video generation jobs until stopped (Ctrl+C).'

    def add_arguments(self, parser):
        parser.add_argument('--poll-seconds', type=float, default=2.0)

    def handle(self, *args, **options):
        # 0 would query the database nonstop; a negative or non-finite value would
        # crash the worker at its first idle wait (time.sleep raises ValueError).
        if not (math.isfinite(options['poll_seconds']) and options['poll_seconds'] > 0):
            raise CommandError('--poll-seconds must be a number greater than 0.')
        try:
            run_forever(poll_seconds=options['poll_seconds'])
        except KeyboardInterrupt:
            self.stdout.write('Worker stopped.')
