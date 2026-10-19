from django.core.management.base import BaseCommand

from apps.generator.worker import run_forever


class Command(BaseCommand):
    help = 'Process queued video generation jobs until stopped (Ctrl+C).'

    def add_arguments(self, parser):
        parser.add_argument('--poll-seconds', type=float, default=2.0)

    def handle(self, *args, **options):
        try:
            run_forever(poll_seconds=options['poll_seconds'])
        except KeyboardInterrupt:
            self.stdout.write('Worker stopped.')
