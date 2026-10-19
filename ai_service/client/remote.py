"""HTTP client for the protected Colab service (see ai_service/colab/server.py).

Contract (all requests send `Authorization: Bearer <AI_SERVICE_API_KEY>`):

    POST /v1/jobs              multipart: `request` (JSON) + optional `image`
                               -> 202 {"job_id": "..."}
    GET  /v1/jobs/{id}         -> {"status": "queued|processing|completed|failed",
                                   "error": {"code", "message"} | null}
    GET  /v1/jobs/{id}/video   -> video/mp4 bytes
    GET  /v1/health            -> {"status": "ok", "gpu": "..."}

Generation takes minutes, so the client submits, polls, then downloads; it
never holds one HTTP request open for the whole generation. Only the standard
library is used, so Django needs no extra dependency.
"""

import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid

from .base import AIServiceError, BaseClient, verify_mp4


class RemoteClient(BaseClient):
    def __init__(self, base_url, api_key, request_timeout=30, poll_interval=10, max_wait=1800):
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

    # -- HTTP helpers -------------------------------------------------------

    def _request(self, method, path, body=None, content_type=None):
        request = urllib.request.Request(f'{self.base_url}{path}', data=body, method=method)
        request.add_header('Authorization', f'Bearer {self.api_key}')
        request.add_header('Accept', 'application/json, video/mp4')
        if content_type:
            request.add_header('Content-Type', content_type)
        try:
            return urllib.request.urlopen(request, timeout=self.request_timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AIServiceError('service_auth_failed', 'The AI service rejected the API key.') from exc
            if exc.code == 422 or exc.code == 400:
                raise AIServiceError('service_rejected', 'The AI service rejected the request.') from exc
            raise AIServiceError(
                'service_error', 'The AI service returned an error.', retryable=exc.code >= 500,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIServiceError(
                'service_unavailable',
                'The AI service is unavailable. Check that the Colab notebook is running.',
                retryable=True,
            ) from exc

    def _json(self, method, path, body=None, content_type=None):
        with self._request(method, path, body, content_type) as response:
            try:
                return json.loads(response.read().decode('utf-8'))
            except (ValueError, UnicodeDecodeError) as exc:
                raise AIServiceError('invalid_response', 'The AI service sent an invalid response.') from exc

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

    # -- Public API ---------------------------------------------------------

    def health(self):
        return self._json('GET', '/v1/health')

    def generate(self, request, output_path, on_submitted=None):
        files = {'image': request.image_path} if request.image_path else {}
        body, content_type = self._multipart({'request': json.dumps(request.payload())}, files)
        submitted = self._json('POST', '/v1/jobs', body, content_type)
        remote_id = submitted.get('job_id')
        if not isinstance(remote_id, str) or not remote_id:
            raise AIServiceError('invalid_response', 'The AI service did not return a job ID.')
        if on_submitted:
            on_submitted(remote_id)

        deadline = time.monotonic() + self.max_wait
        while True:
            state = self._poll(remote_id)
            status = state.get('status')
            if status == 'completed':
                break
            if status == 'failed':
                error = state.get('error') or {}
                raise AIServiceError(
                    str(error.get('code') or 'generation_failed')[:50],
                    str(error.get('message') or 'The model could not generate this video.')[:500],
                )
            if status not in ('queued', 'processing'):
                raise AIServiceError('invalid_response', 'The AI service reported an unknown status.')
            if time.monotonic() > deadline:
                raise AIServiceError('service_timeout', 'The video took too long to generate.')
            time.sleep(self.poll_interval)

        with self._request('GET', f'/v1/jobs/{remote_id}/video') as response, output_path.open('wb') as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        verify_mp4(output_path)

    def _poll(self, remote_id):
        # A single dropped poll (e.g. a tunnel hiccup) should not fail the job.
        for attempt in range(3):
            try:
                return self._json('GET', f'/v1/jobs/{remote_id}')
            except AIServiceError as exc:
                if not exc.retryable or attempt == 2:
                    raise
                time.sleep(self.poll_interval)
