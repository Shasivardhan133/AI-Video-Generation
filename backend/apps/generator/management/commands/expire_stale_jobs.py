from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.generator.worker import expired_queued_jobs, queue_timeout_seconds, recover_stale_jobs


class Command(BaseCommand):
    help = ('Fail queued jobs older than GENERATION_QUEUE_TIMEOUT_SECONDS (queue_timeout) and processing jobs '
            'whose worker stopped sending heartbeats (worker_interrupted). It runs the same recovery cycle as '
            'the workers, so it is safe to run repeatedly and while workers run. Schedule it when no worker '
            'is running, because expiry otherwise happens only inside a worker.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Only report how many queued jobs have passed the queue timeout; change nothing.')

    def handle(self, *args, **options):
        if options['dry_run']:
            count = expired_queued_jobs(timezone.now()).count()
            self.stdout.write(f'{count} queued job(s) have waited longer than {queue_timeout_seconds()} s. '
                              'Dry run: nothing was changed.')
            return
        interrupted, expired = recover_stale_jobs()
        self.stdout.write(f'Expired {expired} queued job(s) (queue_timeout). '
                          f'Recovered {interrupted} interrupted job(s) (worker_interrupted).')
