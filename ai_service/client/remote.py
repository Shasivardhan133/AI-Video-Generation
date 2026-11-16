"""HTTP client for the protected Colab service. The service itself is not part
of this repository yet (there is no ai_service/colab/ folder); this docstring is the contract
it must implement.

Contract (all requests send `Authorization: Bearer <AI_SERVICE_API_KEY>`):

    POST /v1/jobs              multipart: `request` (JSON) + optional `image`
                               -> 202 {"job_id": "..."}
    GET  /v1/jobs/{id}         -> {"status": "queued|processing|completed|failed",
                                   "error": {"code", "message"} | null}
    GET  /v1/jobs/{id}/video   -> video/mp4 bytes
    GET  /v1/health            -> {"status": "ok", "gpu": "..."}

Job IDs. The service's job ID must match REMOTE_JOB_ID: 1 to 64 ASCII letters,
digits, '_' or '-', starting with a letter or digit (a UUID qualifies). IDs
starting with 'mock-' are reserved for the local mock client (the API labels
such output as a test video). Any other value is rejected before it is stored
or put into a URL, and no further request is made for it.

Limits. One generation (submit, polling and download together) must finish
within `max_wait` seconds (AI_SERVICE_MAX_WAIT): every request's timeout is
capped by the time left, polling sleeps never pass the deadline, and the
download checks the deadline after every chunk it receives, so the whole
operation can overrun the deadline by at most one network read (bounded by
the per-request timeout). The video may be at most `max_video_bytes`
(AI_SERVICE_MAX_VIDEO_BYTES): a larger declared Content-Length is refused
before any byte is read, and the bytes actually received are counted, so a
missing or wrong Content-Length cannot get past the limit. JSON replies are
limited to MAX_JSON_BYTES. On any failure the partly written output file is
deleted; nothing else is touched.

Generation takes minutes, so the client submits, polls, then downloads; it
never holds one HTTP request open for the whole generation. Only the standard
library is used, so Django needs no extra dependency.
"""

import http.client
import json
import logging
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .base import AIServiceError, BaseClient, verify_mp4

logger = logging.getLogger(__name__)
# A service error code passed through to the job: the whole string must match
# (fullmatch), so a trailing newline is rejected; 50 fits error_code.
SAFE_ERROR_CODE = re.compile(r'[a-z0-9_]{1,50}', re.ASCII)
# fullmatch + ASCII only: '\Z'-style anchoring, so a trailing newline never slips through.
REMOTE_JOB_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', re.ASCII)
RESERVED_ID_PREFIX = 'mock-'
GENERATION_FAILED_MESSAGE = 'The model could not generate this video.'
TIMEOUT_MESSAGE = 'The video took too long to generate.'
MAX_JSON_BYTES = 64 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_VIDEO_BYTES = 100 * 1024 * 1024
LOGGED_DETAIL_CHARS = 500  # remote error details are logged, but never in full


def is_valid_remote_job_id(value):
    return (isinstance(value, str) and REMOTE_JOB_ID.fullmatch(value) is not None
            and not value.startswith(RESERVED_ID_PREFIX))


class Deadline:
    """The time left for one generation, measured on a monotonic clock."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.ends_at = time.monotonic() + seconds
        # True while the current request's socket timeout is the time left (see _timeout).
        self.caps_request_timeout = False

    def remaining(self):
        return self.ends_at - time.monotonic()

    def expired(self):
        return self.remaining() <= 0

    def check(self, stage):
        if self.expired():
            self.fail(stage)

    def fail(self, stage):
        logger.warning('AI service deadline of %ss reached while %s', self.seconds, stage)
        raise AIServiceError('service_timeout', TIMEOUT_MESSAGE)

    def reached_by(self, exc):
        """After a network error: was it the deadline that ran out?

        When the socket timeout is the time left, a timeout means the deadline
        was reached, even if the monotonic clock is a millisecond short of it
        (socket timeouts can fire slightly early).
        """
        timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, 'reason', None), TimeoutError)
        return self.expired() or (self.caps_request_timeout and timed_out)

    def sleep(self, seconds, stage):
        time.sleep(max(0, min(seconds, self.remaining())))
        self.check(stage)


class RemoteClient(BaseClient):
    def __init__(self, base_url, api_key, request_timeout=30, poll_interval=10, max_wait=1800,
                 max_video_bytes=DEFAULT_MAX_VIDEO_BYTES):
        if not base_url or not api_key:
            raise AIServiceError(
                'service_not_configured',
                'The AI service is not configured. Set AI_SERVICE_BASE_URL and AI_SERVICE_API_KEY.',
            )
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.max_video_bytes = max_video_bytes

    # -- HTTP helpers -------------------------------------------------------

    def _timeout(self, deadline):
        if deadline is None:
            return self.request_timeout
        deadline.check('waiting to send a request')
        remaining = deadline.remaining()
        deadline.caps_request_timeout = remaining < self.request_timeout
        return max(0.001, min(self.request_timeout, remaining))

    def _request(self, method, path, body=None, content_type=None, deadline=None, stage='contacting the AI service'):
        request = urllib.request.Request(f'{self.base_url}{path}', data=body, method=method)
        request.add_header('Authorization', f'Bearer {self.api_key}')
        request.add_header('Accept', 'application/json, video/mp4')
        if content_type:
            request.add_header('Content-Type', content_type)
        try:
            return urllib.request.urlopen(request, timeout=self._timeout(deadline))
        except urllib.error.HTTPError as exc:
            exc.close()
            logger.warning('AI service answered HTTP %s while %s', exc.code, stage)
            if exc.code in (401, 403):
                raise AIServiceError('service_auth_failed', 'The AI service rejected the API key.') from None
            if exc.code == 422 or exc.code == 400:
                raise AIServiceError('service_rejected', 'The AI service rejected the request.') from None
            raise AIServiceError(
                'service_error', 'The AI service returned an error.', retryable=exc.code >= 500,
            ) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            if deadline is not None and deadline.reached_by(exc):
                deadline.fail(stage)
            logger.warning('AI service unreachable while %s (%s)', stage, type(exc).__name__)
            raise AIServiceError(
                'service_unavailable',
                'The AI service is unavailable. Check that the Colab notebook is running.',
                retryable=True,
            ) from None

    def _json(self, method, path, body=None, content_type=None, deadline=None, stage='contacting the AI service'):
        with self._request(method, path, body, content_type, deadline=deadline, stage=stage) as response:
            try:
                raw = response.read(MAX_JSON_BYTES + 1)
            except (http.client.HTTPException, OSError) as exc:
                if deadline is not None and deadline.reached_by(exc):
                    deadline.fail(stage)
                logger.warning('AI service reply could not be read while %s (%s)', stage, type(exc).__name__)
                raise AIServiceError('service_unavailable', 'The AI service is unavailable. Check that the Colab '
                                     'notebook is running.', retryable=True) from None
        try:
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError('reply too large')
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError('reply is not a JSON object')
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning('AI service sent an invalid reply while %s (%s)', stage, exc)
            raise AIServiceError('invalid_response', 'The AI service sent an invalid response.') from None
        return data

    @staticmethod
    def _multipart(fields, files):
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + value.encode('utf-8') + b'\r\n'
            )
        for name, path in files.items():
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
                + path.read_bytes() + b'\r\n'
            )
        parts.append(f'--{boundary}--\r\n'.encode())
        return b''.join(parts), f'multipart/form-data; boundary={boundary}'

    @staticmethod
    def _job_path(remote_id, suffix=''):
        # Validated already; quoting as well means no value can ever change the path.
        return f'/v1/jobs/{urllib.parse.quote(remote_id, safe="")}{suffix}'

    # -- Public API ---------------------------------------------------------

    def health(self):
        return self._json('GET', '/v1/health')

    def generate(self, request, output_path, on_submitted=None):
        deadline = Deadline(self.max_wait)
        files = {'image': request.image_path} if request.image_path else {}
        body, content_type = self._multipart({'request': json.dumps(request.payload())}, files)
        submitted = self._json('POST', '/v1/jobs', body, content_type, deadline=deadline, stage='submitting the job')
        remote_id = submitted.get('job_id')
        if not is_valid_remote_job_id(remote_id):
            # Never stored, logged or used in a URL: the value came from outside and
            # could even echo a secret, so only its type and length are logged.
            length = len(remote_id) if isinstance(remote_id, (str, list, dict)) else '-'
            logger.warning('AI service returned an invalid job ID (type %s, length %s)', type(remote_id).__name__, length)
            raise AIServiceError('invalid_response', 'The AI service did not return a valid job ID.')
        if on_submitted:
            on_submitted(remote_id)

        while True:
            state = self._poll(remote_id, deadline)
            status = state.get('status')
            if status == 'completed':
                break
            if status == 'failed':
                # The service's own message may contain internal details (paths,
                # tracebacks), so users get a fixed message and the log keeps the rest.
                error = state.get('error') if isinstance(state.get('error'), dict) else {}
                code = str(error.get('code') or '')
                logger.warning('AI service job %s failed: %s', remote_id, repr(error)[:LOGGED_DETAIL_CHARS])
                raise AIServiceError(
                    code if SAFE_ERROR_CODE.fullmatch(code) else 'generation_failed',
                    GENERATION_FAILED_MESSAGE,
                )
            if status not in ('queued', 'processing'):
                logger.warning('AI service job %s reported an unknown status %r', remote_id, str(status)[:40])
                raise AIServiceError('invalid_response', 'The AI service reported an unknown status.')
            deadline.sleep(self.poll_interval, 'waiting for the AI service to finish')

        self._download(remote_id, output_path, deadline)

    def _poll(self, remote_id, deadline):
        # A single dropped poll (e.g. a tunnel hiccup) should not fail the job.
        for attempt in range(3):
            try:
                return self._json('GET', self._job_path(remote_id), deadline=deadline, stage='checking the job status')
            except AIServiceError as exc:
                if not exc.retryable or attempt == 2:
                    raise
                deadline.sleep(self.poll_interval, 'retrying a status check')

    def _download(self, remote_id, output_path, deadline):
        """Stream the video to output_path within the size limit and the deadline.

        Any failure (including KeyboardInterrupt or SystemExit) deletes the file
        this call created; a file that was already at output_path is never
        touched unless this call opened it for writing.
        """
        stage = 'downloading the video'
        response = self._request('GET', self._job_path(remote_id, '/video'), deadline=deadline, stage=stage)
        created = False
        try:
            with response:
                declared = response.length  # None when Content-Length is missing or invalid
                if declared is not None and declared > self.max_video_bytes:
                    self._too_large(remote_id, f'declared {declared} bytes')
                received = 0
                with output_path.open('wb') as out:
                    created = True
                    while True:
                        deadline.check(stage)
                        try:
                            chunk = response.read1(DOWNLOAD_CHUNK_BYTES)
                        except (http.client.HTTPException, OSError) as exc:
                            if deadline.reached_by(exc):
                                deadline.fail(stage)
                            logger.warning('AI service job %s: video download failed after %s bytes (%s)',
                                           remote_id, received, type(exc).__name__)
                            raise AIServiceError('download_failed',
                                                 'The video could not be downloaded from the AI service.') from None
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > self.max_video_bytes:
                            self._too_large(remote_id, f'more than {self.max_video_bytes} bytes received')
                        out.write(chunk)
                if declared is not None and received != declared:
                    logger.warning('AI service job %s: video ended after %s of %s bytes', remote_id, received, declared)
                    raise AIServiceError('download_failed', 'The video could not be downloaded from the AI service.')
            verify_mp4(output_path)
        except BaseException:
            if created:
                output_path.unlink(missing_ok=True)
            raise

    def _too_large(self, remote_id, detail):
        logger.warning('AI service job %s: video refused, %s (limit %s bytes)', remote_id, detail, self.max_video_bytes)
        raise AIServiceError('output_too_large', 'The generated video is larger than this application accepts.')
