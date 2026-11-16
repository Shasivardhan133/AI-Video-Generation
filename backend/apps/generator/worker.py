"""Background processing of queued generation jobs.

Web requests only create `queued` jobs. This worker claims them one at a time,
calls the AI service, stores the verified output, and records the final state.
Run it with `python manage.py run_generation_worker`, or let `runserver` start
it in a background thread (RUN_WORKER_IN_PROCESS=True). Several workers may run
at once.

Leases. A claimed job is `processing` and its `updated_at` acts as a heartbeat:
while a worker works on it, a heartbeat thread refreshes `updated_at` every
WORKER_HEARTBEAT_SECONDS. A `processing` job whose heartbeat is older than
WORKER_STALE_AFTER_SECONDS has lost its worker (crash, kill, power loss), and
any worker may fail it. A live worker's job is never failed by another worker,
and a worker only records a result while its job is still `processing`, so a
job that was already recovered is never overwritten.

Database connections. Django connections are per thread and are only replaced
when something closes them, which normally happens at the end of a web request.
The worker is not a request, so it checks its own connections: the heartbeat
thread pings its connection before each heartbeat and closes it after any
error, so a later heartbeat reconnects; the worker thread does the same before
recording a result, which may come many minutes after its last query.
"""

import logging
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import InterfaceError, OperationalError, close_old_connections, connection, transaction
from django.utils import timezone

from ai_service.client import AIServiceError, GenerationRequest, get_client

from .media_files import store_new_file, stored_file_path
from .models import GenerationJob
from .profiles import get_profile

logger = logging.getLogger(__name__)

QUEUED = GenerationJob.Status.QUEUED
PROCESSING = GenerationJob.Status.PROCESSING
INTERRUPTED_CODE = 'worker_interrupted'
INTERRUPTED_MESSAGE = 'Generation was interrupted. Please try again.'
QUEUE_TIMEOUT_CODE = 'queue_timeout'
QUEUE_TIMEOUT_MESSAGE = 'The video could not be started in time. Please try again.'
# Errors after which the connection may be broken (server restart, network
# drop, killed session). Other database errors are not retried.
CONNECTION_ERRORS = (OperationalError, InterfaceError)

STYLE_HINTS = {
    'cinematic': 'cinematic lighting, film look',
    'realistic': 'photorealistic, natural lighting',
    'anime': 'anime style',
    '3d_animation': '3D animation style',
}


def heartbeat_seconds():
    return settings.WORKER_HEARTBEAT_SECONDS


def stale_after_seconds():
    return settings.WORKER_STALE_AFTER_SECONDS


def queue_timeout_seconds():
    return settings.GENERATION_QUEUE_TIMEOUT_SECONDS


def queue_cutoff(now):
    """Jobs created before this moment have waited longer than the queue timeout."""
    return now - timedelta(seconds=queue_timeout_seconds())


def describe_db_error(exc):
    """Short, secret-free description of a database error for the logs."""
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    return f'{type(exc).__name__} {code}' if code is not None else type(exc).__name__


def discard_connection():
    """Close this thread's database connection; the next query opens a new one.

    Never inside a transaction: that would lose the transaction's work. (The
    worker never runs one around this; Django's TestCase does.)
    """
    if connection.in_atomic_block:
        return False
    try:
        connection.close()
    except Exception:  # an already broken connection may fail to close cleanly
        logger.debug('Ignoring error while closing a broken database connection', exc_info=True)
    return True


def drop_broken_connection():
    """Discard this thread's connection if the server no longer answers a ping."""
    if connection.connection is not None and not connection.in_atomic_block and not connection.is_usable():
        logger.info('Database connection was lost; reconnecting')
        discard_connection()


def build_prompt(job):
    prompt = job.prompt_text.strip()
    hint = STYLE_HINTS.get(job.settings.get('style'))
    if hint:
        prompt = f'{prompt}, {hint}' if prompt else hint
    return prompt


def claim_next_job():
    """Atomically move the oldest queued job to `processing`, or return None.

    SELECT ... FOR UPDATE SKIP LOCKED lets concurrent workers claim different
    jobs; setting `processing` also starts the lease (updated_at = now). A job
    past the queue timeout is never started, even before expiry has marked it.
    """
    with transaction.atomic():
        job = (
            GenerationJob.objects.select_for_update(skip_locked=True)
            .filter(status=QUEUED, created_at__gte=queue_cutoff(timezone.now()))
            .order_by('created_at')
            .first()
        )
        if job is None:
            return None
        job.transition_to(PROCESSING)
        return job


def recover_interrupted_jobs(now=None):
    """Fail `processing` jobs whose worker has stopped sending heartbeats.

    One conditional UPDATE, so a job whose heartbeat arrives first is skipped
    and two workers recovering at once fail each job only once. Returns the count.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=stale_after_seconds())
    count = GenerationJob.objects.filter(status=PROCESSING, updated_at__lt=cutoff).update(
        status=GenerationJob.Status.FAILED,
        error_code=INTERRUPTED_CODE,
        error_message=INTERRUPTED_MESSAGE,
        completed_at=now,
        updated_at=now,
    )
    if count:
        logger.warning('Marked %s abandoned job(s) as failed', count)
    return count


def expired_queued_jobs(now):
    return GenerationJob.objects.filter(status=QUEUED, created_at__lt=queue_cutoff(now))


def expire_queued_jobs(now=None):
    """Fail `queued` jobs that have waited longer than the queue timeout.

    One conditional UPDATE (WHERE status = 'queued'): a job a worker is claiming
    is row-locked, so this UPDATE waits for the claim to commit and then skips
    the job, which is `processing` by then. A job is therefore never expired
    after it has started, and repeated or concurrent runs expire each job only
    once. Returns the count.
    """
    now = now or timezone.now()
    count = expired_queued_jobs(now).update(
        status=GenerationJob.Status.FAILED,
        error_code=QUEUE_TIMEOUT_CODE,
        error_message=QUEUE_TIMEOUT_MESSAGE,
        completed_at=now,
        updated_at=now,
    )
    if count:
        logger.warning('Marked %s job(s) that waited too long in the queue as failed', count)
    return count


def recover_stale_jobs(now=None):
    """The whole recovery cycle: abandoned `processing` jobs and expired `queued` jobs.

    Returns (interrupted, expired) counts.
    """
    now = now or timezone.now()
    return recover_interrupted_jobs(now=now), expire_queued_jobs(now=now)


def finish_job(job, new_status, **fields):
    """Record a job's final state only if it is still `processing` (still ours).

    Returns False when the result was not recorded: another worker already
    recovered the job, or the job no longer exists (deleted while it was being
    processed). The database is then left as it is.

    If the connection fails during the write, the write may or may not have
    committed, so it is not simply repeated: the job is re-read on a fresh
    connection. Still `processing` means the write did not happen and it is
    retried once (still only while `processing`); already holding exactly
    these values means it did. If the job cannot be re-read, the error is
    raised and the outcome is unknown.
    """
    now = timezone.now()
    values = {'status': new_status, 'completed_at': now, 'updated_at': now, **fields}
    if 'error_message' in values:
        values['error_message'] = values['error_message'][:500]
    try:
        drop_broken_connection()  # the last query may have been many minutes ago
        updated = write_if_processing(job, values)
    except CONNECTION_ERRORS as exc:
        if not discard_connection():
            raise
        logger.warning('Job %s: connection failed while recording %s (%s); reconnecting to check the job',
                       job.id, new_status, describe_db_error(exc))
        updated = recheck_and_retry(job, values)
    if not updated:
        try:
            job.refresh_from_db()
        except GenerationJob.DoesNotExist:
            logger.warning('Job %s no longer exists (deleted while processing); result not recorded', job.id)
            return False
        logger.warning('Job %s was no longer processing (lease lost); result not recorded', job.id)
        return False
    for name, value in values.items():
        setattr(job, name, value)
    return True


def write_if_processing(job, values):
    return GenerationJob.objects.filter(pk=job.pk, status=PROCESSING).update(**values)


def recheck_and_retry(job, values):
    """After a failed write: 1 if `values` are (now) recorded, 0 if the job was finished by
    someone else or no longer exists."""
    current = GenerationJob.objects.filter(pk=job.pk).first()
    if current is None:
        return 0  # deleted: there is nothing to record the result on
    if current.status == PROCESSING:
        return write_if_processing(job, values)  # the first write did not commit
    ours = all(getattr(current, name) == value for name, value in values.items()
               if name not in ('completed_at', 'updated_at'))
    if ours:
        logger.info('Job %s: the first write had been recorded', job.id)
    return int(ours)


class JobHeartbeat:
    """Refreshes a processing job's `updated_at` from a background thread.

    Retry strategy: exactly one attempt per interval, never faster. After a
    failed attempt the thread's connection is discarded, so the next attempt
    reconnects; attempts continue until the job finishes or the database
    reports that it is no longer `processing` (recovered). The lease is only
    renewed by a conditional UPDATE, so a recovered job is never revived. If
    attempts keep failing for longer than WORKER_STALE_AFTER_SECONDS, an error
    is logged once: the lease has probably expired and the result may be discarded.
    """

    ALIVE, LOST, FAILED = 'alive', 'lost', 'failed'

    def __init__(self, job_id, interval):
        self.job_id = job_id
        self.interval = interval
        self.lost = False
        self.failures = 0  # consecutive failed attempts
        self._last_renewed = time.monotonic()
        self._expiry_reported = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f'heartbeat-{job_id}', daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join(timeout=10)
        return False

    def renew(self):
        """Extend the lease, but only while the job is still `processing`."""
        return GenerationJob.objects.filter(pk=self.job_id, status=PROCESSING).update(updated_at=timezone.now())

    def beat(self):
        """One heartbeat attempt on this thread's connection; returns ALIVE, LOST or FAILED."""
        try:
            drop_broken_connection()
            alive = self.renew()
        except Exception as exc:
            self.failures += 1
            discard_connection()
            if self.failures == 1:
                logger.exception('Heartbeat for job %s failed; retrying every %ss', self.job_id, self.interval)
            else:
                logger.warning('Heartbeat for job %s failed again (%s, %s in a row)',
                               self.job_id, describe_db_error(exc), self.failures)
            if not self._expiry_reported and time.monotonic() - self._last_renewed > stale_after_seconds():
                self._expiry_reported = True
                logger.error('Job %s: no heartbeat for over %ss; its lease has probably expired',
                             self.job_id, stale_after_seconds())
            return self.FAILED
        if not alive:
            self.lost = True
            logger.warning('Job %s is no longer processing; heartbeat stopped', self.job_id)
            return self.LOST
        if self.failures:
            logger.info('Heartbeat for job %s recovered after %s failed attempt(s)', self.job_id, self.failures)
        self.failures, self._last_renewed, self._expiry_reported = 0, time.monotonic(), False
        return self.ALIVE

    def _run(self):
        try:
            while not self._stop.wait(self.interval):
                if self.beat() == self.LOST:
                    return
        finally:
            discard_connection()  # this thread's own database connection


class InvalidJob(Exception):
    """A claimed job cannot be turned into an AI request; it fails with `code`."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


UNSUPPORTED_PROFILE = InvalidJob('unsupported_setting', 'This model profile is no longer available.')
UNSUPPORTED_SETTINGS = InvalidJob('unsupported_setting',
                                  "This job's settings are no longer supported. Please submit a new request.")
INVALID_SETTINGS = InvalidJob('invalid_job_settings',
                              "This job's stored settings are invalid. Please submit a new request.")
INPUT_MISSING = InvalidJob('input_missing', 'The uploaded image is no longer available. Please submit a new request.')


def _stored_int(settings_, name):
    value = settings_.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise INVALID_SETTINGS
    return value


def prepare_request(job):
    """Check a claimed job's stored settings and input, and build its AI request.

    Settings were validated when the job was created, but the stored row may
    since have been edited, or the profile changed or the image removed, so they
    are checked again before any AI work. Raises InvalidJob; the AI client is
    never called for a job that fails here.
    """
    stored = job.settings
    if not isinstance(stored, dict):
        raise INVALID_SETTINGS
    profile = get_profile(stored.get('model_profile'))
    if profile is None or profile.mode != job.mode:
        raise UNSUPPORTED_PROFILE
    width, height = _stored_int(stored, 'width'), _stored_int(stored, 'height')
    frame_count, fps = _stored_int(stored, 'frame_count'), _stored_int(stored, 'fps')
    seed = stored.get('seed')
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise INVALID_SETTINGS
    enhance_prompt = stored.get('enhance_prompt', False)
    if not isinstance(enhance_prompt, bool) or not isinstance(job.prompt_text, str):
        raise INVALID_SETTINGS
    if ((width, height) not in profile.sizes or frame_count not in profile.allowed_frame_counts()
            or fps != profile.fps or (enhance_prompt and not profile.supports_prompt_enhancement)):
        raise UNSUPPORTED_SETTINGS

    image_path = None
    if job.mode == GenerationJob.Mode.IMAGE_TO_VIDEO:
        # Never send the service a file outside MEDIA_ROOT (traversal, a link to outside) or a directory.
        if stored_file_path(job.input_file) is None:
            raise INPUT_MISSING
        image_path = Path(job.input_file.path)

    return GenerationRequest(
        job_id=str(job.id),
        mode=job.mode,
        prompt=build_prompt(job),
        wan_task=profile.wan_task,
        width=width,
        height=height,
        frame_count=frame_count,
        fps=fps,
        seed=seed,
        enhance_prompt=enhance_prompt,
        sample_options=profile.sample_options,
        image_path=image_path,
    )


def process_job(job, client=None):
    # Preparation errors fail the job at once: before this, an unexpected error
    # here escaped the worker and left the job `processing`, with no heartbeat,
    # until stale-lease recovery failed it minutes later as `worker_interrupted`.
    try:
        request = prepare_request(job)
    except InvalidJob as exc:
        logger.warning('Job %s cannot be processed: %s (request %s)', job.id, exc.code, job.request_id)
        finish_job(job, GenerationJob.Status.FAILED, error_code=exc.code, error_message=exc.message)
        return
    except Exception:
        logger.exception('Job %s could not be prepared (request %s)', job.id, job.request_id)
        finish_job(job, GenerationJob.Status.FAILED, error_code='internal_error',
                   error_message='An unexpected error occurred while preparing the video.')
        return

    def remember_remote_id(remote_id):
        GenerationJob.objects.filter(pk=job.pk, status=PROCESSING).update(
            remote_job_id=remote_id[:100], updated_at=timezone.now(),
        )
        job.remote_job_id = remote_id[:100]

    try:
        client = client or get_client(settings)
        with JobHeartbeat(job.pk, heartbeat_seconds()), tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / f'{job.id}.mp4'
            client.generate(request, output_path, on_submitted=remember_remote_id)
            with output_path.open('rb') as handle:
                # A new name (never an existing file); a partly written copy is removed on failure.
                saved_name = store_new_file(job.output_file.field, job, f'{job.id}.mp4', File(handle))
        storage = job.output_file.storage
    except AIServiceError as exc:
        logger.warning('Job %s failed: %s (request %s)', job.id, exc.code, job.request_id)
        finish_job(job, GenerationJob.Status.FAILED, error_code=exc.code, error_message=exc.message)
        return
    except Exception:
        logger.exception('Job %s crashed (request %s)', job.id, job.request_id)
        finish_job(job, GenerationJob.Status.FAILED, error_code='internal_error',
                   error_message='An unexpected error occurred while generating the video.')
        return
    record_output(job, saved_name, storage)


def record_output(job, saved_name, storage):
    """Mark the job completed with the file just saved for it.

    `saved_name` is the name storage gave this write (never an existing file),
    so deleting it can only remove this attempt's own output.
    """
    try:
        recorded = finish_job(job, GenerationJob.Status.COMPLETED, output_file=saved_name)
    except Exception as exc:
        # Unknown outcome: keep the file and leave the job as it is. Its lease
        # expires and recovery fails it; the file is left for an operator.
        logger.error('Job %s: could not record its result (%s); output file %s kept, job left for recovery '
                     '(request %s)', job.id, describe_db_error(exc), saved_name, job.request_id)
        return
    if recorded:
        logger.info('Job %s completed', job.id)
        return
    # The job was recovered or deleted meanwhile (finish_job logged which); remove
    # only the file just written for it.
    try:
        storage.delete(saved_name)
    except OSError as exc:
        logger.error('Job %s: could not delete discarded output %s (%s)', job.id, saved_name, type(exc).__name__)
        return
    logger.info('Job %s: discarded output %s deleted', job.id, saved_name)


def run_forever(poll_seconds=2, stop_event=None):
    recover_stale_jobs()
    logger.info('Generation worker started (mode: %s)', settings.AI_SERVICE_MODE)
    last_recovery = time.monotonic()
    while not (stop_event and stop_event.is_set()):
        close_old_connections()
        # Jobs abandoned by a crashed worker, and jobs that waited too long in the
        # queue, are failed while this worker runs.
        if time.monotonic() - last_recovery >= settings.WORKER_RECOVERY_INTERVAL_SECONDS:
            last_recovery = time.monotonic()
            try:
                recover_stale_jobs()
            except Exception:
                logger.exception('Could not recover abandoned or expired jobs; retrying later')
        try:
            job = claim_next_job()
        except Exception:
            logger.exception('Could not claim a job; retrying')
            job = None
        if job is None:
            if stop_event:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)
            continue
        try:
            process_job(job)
        except (KeyboardInterrupt, SystemExit):
            # Stopped mid-job (for example Ctrl+C): fail it now rather than
            # leaving it for stale-lease recovery, then stop.
            finish_job(job, GenerationJob.Status.FAILED, error_code=INTERRUPTED_CODE,
                       error_message=INTERRUPTED_MESSAGE)
            raise
        except Exception:
            # The job keeps its lease until it goes stale and is recovered.
            logger.exception('Worker error while processing job %s; continuing', job.id)


def start_in_background():
    thread = threading.Thread(target=run_forever, name='generation-worker', daemon=True)
    thread.start()
    return thread
