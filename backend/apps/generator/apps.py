import os
import sys

from django.apps import AppConfig
from django.conf import settings


class GeneratorConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.generator'
    label = 'generator'
    verbose_name = 'Video generation'

    def ready(self):
        # Under `runserver`, RUN_MAIN is set only in the reloaded child process,
        # so the worker thread starts once rather than twice.
        is_runserver = len(sys.argv) > 1 and sys.argv[1] == 'runserver'
        if settings.RUN_WORKER_IN_PROCESS and is_runserver and os.environ.get('RUN_MAIN') == 'true':
            from .worker import start_in_background

            start_in_background()
