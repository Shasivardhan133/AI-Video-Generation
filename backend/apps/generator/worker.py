"""Background processing of queued generation jobs.

Web requests only create `queued` jobs. This worker claims them one at a time,
calls the AI service, stores the verified output, and records the final state.
Run it with `python manage.py run_generation_worker`, or let `runserver` start
it in a background thread (RUN_WORKER_IN_PROCESS=True).
"""

import logging
import tempfile
import threading
import time
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import close_old_connections, transaction

from ai_service.client import AIServiceError, GenerationRequest, get_client

from .models import GenerationJob
from .profiles import get_profile

logger = logging.getLogger(__name__)

STYLE_HINTS = {
    'cinematic': 'cinematic lighting, film look',
    'realistic': 'photorealistic, natural lighting',
    'anime': 'anime style',
    '3d_animation': '3D animation style',
}


def build_prompt(job):
    prompt = job.prompt_text.strip()
    hint = STYLE_HINTS.get(job.settings.get('style'))
    if hint:
        prompt = f'{prompt}, {hint}' if prompt else hint
    return prompt


def claim_next_job():
    """Atomically move the oldest queued job to `processing`, or return None."""
    with transaction.atomic():
        job = (
            GenerationJob.objects.select_for_update(skip_locked=True)
            .filter(status=GenerationJob.Status.QUEUED)
            .order_by('created_at')
            .first()
        )
        if job is None:
            return None
        job.transition_to(GenerationJob.Status.PROCESSING)
        return job


def recover_interrupted_jobs():
    """Jobs left in `processing` by a crashed worker can never finish."""
    count = 0
    for job in GenerationJob.objects.filter(status=GenerationJob.Status.PROCESSING):
        job.fail('worker_interrupted', 'Generation was interrupted. Please try again.')
        count += 1
    if count:
        logger.warning('Marked %s interrupted job(s) as failed', count)


def process_job(job, client=None):
    profile = get_profile(job.settings.get('model_profile'))
    if profile is None:
        job.fail('unsupported_setting', 'This model profile is no longer available.')
        return

    request = GenerationRequest(
        job_id=str(job.id),
        mode=job.mode,
        prompt=build_prompt(job),
        wan_task=profile.wan_task,
        width=job.settings['width'],
        height=job.settings['height'],
        frame_count=job.settings['frame_count'],
        fps=job.settings['fps'],
        seed=job.settings.get('seed'),
        enhance_prompt=job.settings.get('enhance_prompt', False),
        sample_options=profile.sample_options,
        image_path=Path(job.input_file.path) if job.input_file else None,
    )

    def remember_remote_id(remote_id):
        job.remote_job_id = remote_id[:100]
        job.save(update_fields=['remote_job_id', 'updated_at'])

    try:
        client = client or get_client(settings)
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / f'{job.id}.mp4'
            client.generate(request, output_path, on_submitted=remember_remote_id)
            with output_path.open('rb') as handle:
                job.output_file.save(f'{job.id}.mp4', File(handle), save=False)
        job.transition_to(GenerationJob.Status.COMPLETED, output_file=job.output_file)
        logger.info('Job %s completed', job.id)
    except AIServiceError as exc:
        logger.warning('Job %s failed: %s (request %s)', job.id, exc.code, job.request_id)
        job.fail(exc.code, exc.message)
    except Exception:
        logger.exception('Job %s crashed (request %s)', job.id, job.request_id)
        job.fail('internal_error', 'An unexpected error occurred while generating the video.')


def run_forever(poll_seconds=2, stop_event=None):
    recover_interrupted_jobs()
    logger.info('Generation worker started (mode: %s)', settings.AI_SERVICE_MODE)
    while not (stop_event and stop_event.is_set()):
        close_old_connections()
        try:
            job = claim_next_job()
        except Exception:
            logger.exception('Could not claim a job; retrying')
            job = None
        if job is None:
            time.sleep(poll_seconds)
            continue
        process_job(job)


def start_in_background():
    thread = threading.Thread(target=run_forever, name='generation-worker', daemon=True)
    thread.start()
    return thread
