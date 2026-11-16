"""3.5.5: the remote AI client against a local fake service (127.0.0.1, random port).

Real sockets, so Content-Length handling, slow or stalled transfers and closed
connections behave as they would with the Colab service. No real AI service,
no credentials, and only the temporary test database and media folder.
"""

import json
import logging
import shutil
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from django.core.files.base import ContentFile
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from ai_service.client import AIServiceError, GenerationRequest
from ai_service.client import remote
from ai_service.client.remote import RemoteClient, is_valid_remote_job_id
from apps.generator.models import GenerationJob
from apps.generator.worker import process_job, recover_interrupted_jobs

from .base import MP4, PNG, TempMediaTestCase, TempMediaTransactionTestCase

S = GenerationJob.Status
API_KEY = 'secret-test-key-4c1d9a'
VIDEO = b'\x00\x00\x00\x18ftypisom' + bytes(i % 251 for i in range(4000))  # 4012 bytes


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'  # one request per connection; a body may end at close

    def log_message(self, *args):
        pass  # keep the test output clean

    def _record(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else b''
        self.server.service.requests.append((self.command, self.path, dict(self.headers), body))

    def do_POST(self):
        self._record()
        if self.path == '/v1/jobs':
            return self.server.service.submit(self)
        self.send_json(404, {})

    def do_GET(self):
        self._record()
        parts = self.path.strip('/').split('/')
        if parts[:2] == ['v1', 'jobs'] and len(parts) == 3:
            return self.server.service.poll(self)
        if parts[:2] == ['v1', 'jobs'] and len(parts) == 4 and parts[3] == 'video':
            return self.server.service.video(self)
        self.send_json(404, {})

    # Response helpers used by the scripted behaviours.
    def send_json(self, status, data=None, raw=None):
        body = raw if raw is not None else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_video(self, data, length='auto', chunk=None, delay=0.0):
        self.send_response(200)
        self.send_header('Content-Type', 'video/mp4')
        if length == 'auto':
            self.send_header('Content-Length', str(len(data)))
        elif length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()
        try:
            step = chunk or len(data) or 1
            for start in range(0, len(data), step):
                self.wfile.write(data[start:start + step])
                self.wfile.flush()
                if delay:
                    time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the client stopped reading: expected in the limit tests


class FakeService:
    """Scripted behaviour: submit(handler), poll(handler) and video(handler)."""

    def __init__(self):
        self.requests = []
        self.polls = []  # replies for successive status checks; then 'completed'
        self.submit = lambda h: h.send_json(202, {'job_id': 'remote-1'})
        self.video = lambda h: h.send_video(VIDEO)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeHandler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.server.service = self
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()

    def poll(self, handler):
        reply = self.polls.pop(0) if self.polls else {'status': 'completed', 'error': None}
        if callable(reply):
            return reply(handler)
        if isinstance(reply, int):
            return handler.send_json(reply, {})
        handler.send_json(200, reply)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    @property
    def paths(self):
        return [(method, path) for method, path, _, _ in self.requests]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def text_request(**fields):
    return GenerationRequest(**{
        'job_id': str(uuid.uuid4()), 'mode': 'text_to_video', 'prompt': 'A calm lake', 'wan_task': 't2v-1.3B',
        'width': 832, 'height': 480, 'frame_count': 81, 'fps': 16, **fields,
    })


class RemoteTestMixin:
    def setUp(self):
        super().setUp()
        # The client logs warnings on every failure; keep them off the console
        # (Python's last-resort handler). assertLogs still captures them.
        quiet, remote_logger = logging.NullHandler(), logging.getLogger('ai_service.client.remote')
        remote_logger.addHandler(quiet)
        self.addCleanup(remote_logger.removeHandler, quiet)
        self.service = FakeService()
        self.addCleanup(self.service.stop)
        self.out_dir = Path(tempfile.mkdtemp(prefix='geetha-test-remote-'))
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.output = self.out_dir / 'job.mp4'

    def remote_client(self, **options):
        options = {'request_timeout': 2, 'poll_interval': 0, 'max_wait': 10, **options}
        return RemoteClient(self.service.url, API_KEY, **options)

    def generate(self, request=None, **options):
        submitted = []
        self.remote_client(**options).generate(request or text_request(), self.output, on_submitted=submitted.append)
        return submitted

    def assertFails(self, code, request=None, **options):
        """Generation fails with `code`, leaves no output file and never shows the key."""
        with self.assertLogs('ai_service.client.remote', level='DEBUG') as logs, \
                self.assertRaises(AIServiceError) as ctx:
            logging.getLogger('ai_service.client.remote').debug('start')  # assertLogs needs one record
            self.generate(request, **options)
        self.assertEqual(ctx.exception.code, code)
        self.assertFalse(self.output.exists())
        text = '\n'.join(logs.output) + ctx.exception.message
        self.assertNotIn(API_KEY, text)
        return ctx.exception, logs.output


class RemoteClientSuccessTests(RemoteTestMixin, SimpleTestCase):
    def test_text_to_video(self):
        request = text_request(seed=7)
        submitted = self.generate(request)
        self.assertEqual(submitted, ['remote-1'])
        self.assertEqual(self.output.read_bytes(), VIDEO)
        self.assertEqual(self.service.paths, [('POST', '/v1/jobs'), ('GET', '/v1/jobs/remote-1'),
                                              ('GET', '/v1/jobs/remote-1/video')])
        for _, _, headers, _ in self.service.requests:
            self.assertEqual(headers['Authorization'], f'Bearer {API_KEY}')
        body = self.service.requests[0][3]
        self.assertIn(b'name="request"', body)
        self.assertIn(json.dumps(request.payload()).encode(), body)
        self.assertNotIn(b'name="image"', body)

    def test_image_to_video_uploads_the_image(self):
        image = self.out_dir / 'cat-0123abcd.png'
        image.write_bytes(PNG)
        request = text_request(mode='image_to_video', wan_task='i2v-14B', image_path=image)
        self.generate(request)
        body = self.service.requests[0][3]
        self.assertIn(b'name="image"; filename="cat-0123abcd.png"\r\nContent-Type: image/png\r\n\r\n' + PNG, body)
        self.assertIn(b'"mode": "image_to_video"', body)
        self.assertTrue(image.exists())  # the input is never removed

    def test_polls_until_completed(self):
        self.service.polls = [{'status': 'queued'}, {'status': 'processing'}, {'status': 'processing'}]
        self.generate()
        self.assertEqual([p for _, p in self.service.paths].count('/v1/jobs/remote-1'), 4)

    def test_valid_job_id_formats(self):
        for remote_id in (str(uuid.uuid4()), 'A', 'job_42-x', 'a' * 64, uuid.uuid4().hex):
            with self.subTest(remote_id=remote_id):
                self.service.submit = lambda h, rid=remote_id: h.send_json(202, {'job_id': rid})
                self.assertEqual(self.generate(), [remote_id])

    def test_video_exactly_at_the_limit_is_accepted(self):
        self.generate(max_video_bytes=len(VIDEO))
        self.assertEqual(self.output.read_bytes(), VIDEO)

    def test_missing_or_invalid_content_length_is_counted(self):
        for length in (None, 'abc', '-5'):
            with self.subTest(length=length):
                self.service.video = lambda h, n=length: h.send_video(VIDEO, length=n, chunk=1000)
                self.generate(max_video_bytes=len(VIDEO))
                self.assertEqual(self.output.read_bytes(), VIDEO)

    def test_a_too_small_content_length_never_lets_more_than_it_through(self):
        # The service declares 3000 bytes but sends more: only 3000 are read.
        self.service.video = lambda h: h.send_video(VIDEO + b'x' * 10_000, length=3000)
        self.generate(max_video_bytes=3500)
        self.assertEqual(self.output.read_bytes(), (VIDEO + b'x' * 10_000)[:3000])


class RemoteClientFailureTests(RemoteTestMixin, SimpleTestCase):
    # Submission
    def test_submission_timeout(self):
        self.service.submit = lambda h: time.sleep(2)
        started = time.monotonic()
        exc, _ = self.assertFails('service_unavailable', request_timeout=0.5)
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertTrue(exc.retryable)

    def test_unreachable_service(self):
        self.service.stop()
        self.assertFails('service_unavailable')

    def test_http_errors_on_submission(self):
        for status, code in ((401, 'service_auth_failed'), (403, 'service_auth_failed'), (400, 'service_rejected'),
                             (422, 'service_rejected'), (500, 'service_error'), (503, 'service_error'),
                             (404, 'service_error')):
            with self.subTest(status=status):
                self.service.requests.clear()
                self.service.submit = lambda h, s=status: h.send_json(s, {'detail': 'internal /content/x.py'})
                _, logs = self.assertFails(code)
                self.assertEqual(self.service.paths, [('POST', '/v1/jobs')])
                self.assertFalse(any('/content/x.py' in line for line in logs))  # error bodies are not logged

    def test_malformed_or_unexpected_json(self):
        replies = {
            'not json': b'not json', 'array': b'[1, 2]', 'string': b'"remote-1"', 'number': b'7', 'empty': b'',
            'invalid utf-8': b'{"job_id": "\xff"}', 'too large': b'{"pad": "' + b'x' * (70 * 1024) + b'"}',
        }
        for name, raw in replies.items():
            with self.subTest(name):
                self.service.requests.clear()
                self.service.submit = lambda h, r=raw: h.send_json(202, raw=r)
                self.assertFails('invalid_response')
                self.assertEqual(self.service.paths, [('POST', '/v1/jobs')])

    def test_missing_or_invalid_job_ids_stop_before_any_further_request(self):
        bad_ids = [None, '', 123, ['remote-1'], {'id': 1}, True, '../admin', 'a/b', '..', '.', 'a?b', 'a#b', 'a b',
                   'a%2Fb', 'a\nb', 'remote-1\n', '-leading-dash', '_x', 'x' * 65, 'café', '１', 'mock-123']
        for remote_id in bad_ids:
            with self.subTest(remote_id=remote_id):
                self.service.requests.clear()
                self.service.submit = lambda h, rid=remote_id: h.send_json(
                    202, {'job_id': rid} if rid is not None else {'other': 1})
                submitted = []
                with self.assertLogs('ai_service.client.remote', level='WARNING') as logs, \
                        self.assertRaises(AIServiceError) as ctx:
                    self.remote_client().generate(text_request(), self.output, on_submitted=submitted.append)
                self.assertEqual(ctx.exception.code, 'invalid_response')
                self.assertEqual(submitted, [])  # never stored
                self.assertEqual(self.service.paths, [('POST', '/v1/jobs')])  # no further authenticated request
                self.assertTrue(all('\n' not in line.split(':', 2)[-1] for line in logs.output))  # no log injection
                if isinstance(remote_id, str) and len(remote_id) > 1:
                    self.assertFalse(any(remote_id in line for line in logs.output))  # the value is never logged

    def test_validator(self):
        self.assertTrue(is_valid_remote_job_id('abc-123_X'))
        for value in ('', 'mock-1', 'a' * 65, 'a/b', 'a\n', None, 5):
            self.assertFalse(is_valid_remote_job_id(value), value)

    # Polling
    def test_unknown_statuses(self):
        for state in ({'status': 'done'}, {'status': 'COMPLETED'}, {'status': None}, {}, {'status': 3},
                      {'status': ['completed']}):
            with self.subTest(state=state):
                self.service.polls = [state]
                self.assertFails('invalid_response')

    def test_failed_job_keeps_details_in_the_log_only(self):
        self.service.polls = [{'status': 'failed', 'error': {'code': 'out_of_memory', 'message': 'x' * 5000}}]
        exc, logs = self.assertFails('out_of_memory')
        self.assertEqual(exc.message, 'The model could not generate this video.')
        self.assertTrue(all(len(line) < 800 for line in logs))  # the remote detail is truncated

    def test_service_error_codes_must_match_exactly(self):
        # 3.5.10: the service's code is passed through only if the whole string is
        # 1-50 of [a-z0-9_]. `re.match` with '$' used to accept a trailing newline.
        cases = {
            'out_of_memory': 'out_of_memory',          # valid
            'a' * 50: 'a' * 50,                        # valid, at the length limit
            'Out_Of_Memory': 'generation_failed',      # upper case
            'out-of-memory': 'generation_failed',      # invalid character
            'oom!': 'generation_failed',
            'oom\n': 'generation_failed',              # trailing newline
            'a' * 50 + '\n': 'generation_failed',
            '\noom': 'generation_failed',
            '': 'generation_failed',                   # empty
            None: 'generation_failed',                 # missing
            'a' * 51: 'generation_failed',             # too long
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.service.polls = [{'status': 'failed', 'error': {'code': code, 'message': 'internal detail'}}]
                exc, _ = self.assertFails(expected)
                self.assertEqual(exc.message, 'The model could not generate this video.')  # never the service's

    def test_transient_poll_errors_are_retried(self):
        self.service.polls = [503, 502, {'status': 'processing'}, 500, {'status': 'completed'}]
        self.generate()
        self.assertEqual(self.output.read_bytes(), VIDEO)

    def test_three_failed_polls_in_a_row_fail(self):
        self.service.polls = [503, 503, 503, {'status': 'completed'}]
        self.assertFails('service_error')
        self.assertEqual([p for _, p in self.service.paths].count('/v1/jobs/remote-1'), 3)

    def test_permanent_poll_errors_are_not_retried(self):
        for reply, code in ((404, 'service_error'), (401, 'service_auth_failed'),
                            (lambda h: h.send_json(200, raw=b'oops'), 'invalid_response')):
            with self.subTest(code=code):
                self.service.requests.clear()
                self.service.polls = [reply]
                self.assertFails(code)
                self.assertEqual([p for _, p in self.service.paths].count('/v1/jobs/remote-1'), 1)

    # Overall deadline
    def test_deadline_while_polling(self):
        self.service.polls = [{'status': 'processing'}] * 1000
        started = time.monotonic()
        exc, logs = self.assertFails('service_timeout', max_wait=1, poll_interval=0.2)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(exc.message, 'The video took too long to generate.')
        self.assertTrue(any('deadline of 1s reached' in line for line in logs))

    def test_deadline_while_retrying_polls(self):
        self.service.polls = [503] * 1000
        started = time.monotonic()
        self.assertFails('service_timeout', max_wait=1, poll_interval=5)  # the retry sleep is cut short
        self.assertLess(time.monotonic() - started, 2.5)

    def test_deadline_during_submission(self):
        self.service.submit = lambda h: time.sleep(3)
        started = time.monotonic()
        self.assertFails('service_timeout', max_wait=1, request_timeout=30)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_deadline_during_a_slow_download(self):
        self.service.video = lambda h: h.send_video(VIDEO * 50, chunk=2000, delay=0.2)  # about 20 s in total
        started = time.monotonic()
        self.assertFails('service_timeout', max_wait=1, request_timeout=5)
        self.assertLess(time.monotonic() - started, 2.5)

    # Download
    def test_download_stall_times_out(self):
        def stall(handler):
            handler.send_response(200)
            handler.send_header('Content-Length', str(len(VIDEO)))
            handler.end_headers()
            handler.wfile.write(VIDEO[:500])
            handler.wfile.flush()
            time.sleep(3)

        self.service.video = stall
        started = time.monotonic()
        self.assertFails('download_failed', request_timeout=0.5)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_http_error_on_download(self):
        self.service.video = lambda h: h.send_json(500, {})
        self.assertFails('service_error')

    def test_declared_size_over_the_limit_is_refused_before_reading(self):
        self.service.video = lambda h: h.send_video(VIDEO)
        _, logs = self.assertFails('output_too_large', max_video_bytes=len(VIDEO) - 1)
        self.assertTrue(any(f'declared {len(VIDEO)} bytes' in line for line in logs))

    def test_received_bytes_over_the_limit_without_content_length(self):
        self.service.video = lambda h: h.send_video(VIDEO * 20, length=None, chunk=4096)
        _, logs = self.assertFails('output_too_large', max_video_bytes=10_000)
        self.assertTrue(any('more than 10000 bytes received' in line for line in logs))

    def test_received_bytes_over_the_limit_with_an_invalid_content_length(self):
        self.service.video = lambda h: h.send_video(VIDEO * 20, length='not-a-number', chunk=4096)
        self.assertFails('output_too_large', max_video_bytes=10_000)

    def test_content_length_larger_than_the_body(self):
        self.service.video = lambda h: h.send_video(VIDEO, length=len(VIDEO) + 5000)  # connection closes early
        _, logs = self.assertFails('download_failed')
        self.assertTrue(any(f'after {len(VIDEO)} of {len(VIDEO) + 5000} bytes' in line for line in logs))

    def test_invalid_mp4_output(self):
        for data in (b'\x00' * 3000, b'tiny', b''):
            with self.subTest(size=len(data)):
                self.service.video = lambda h, d=data: h.send_video(d)
                self.assertFails('invalid_output')

    # Cleanup
    def test_partial_file_removed_but_unrelated_files_kept(self):
        neighbour = self.out_dir / 'other-job.mp4'
        neighbour.write_bytes(MP4)
        self.service.video = lambda h: h.send_video(VIDEO * 20, length=None, chunk=4096)
        self.assertFails('output_too_large', max_video_bytes=10_000)
        self.assertEqual(neighbour.read_bytes(), MP4)
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()), ['other-job.mp4'])

    def test_existing_file_untouched_when_failing_before_the_download(self):
        self.output.write_bytes(b'not created by this call')
        self.service.submit = lambda h: h.send_json(503, {})
        with self.assertRaises(AIServiceError):
            self.generate()
        self.assertEqual(self.output.read_bytes(), b'not created by this call')

    def test_interrupted_download_is_removed(self):
        calls = {'n': 0}
        real_check = remote.Deadline.check

        def interrupt_on_third_chunk(deadline, stage):
            if stage == 'downloading the video':
                calls['n'] += 1
                if calls['n'] == 3:
                    raise KeyboardInterrupt
            return real_check(deadline, stage)

        self.service.video = lambda h: h.send_video(VIDEO * 20, chunk=4096, delay=0.01)
        with mock.patch.object(remote.Deadline, 'check', interrupt_on_third_chunk), \
                self.assertRaises(KeyboardInterrupt):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_key_never_logged(self):
        scenarios = [
            lambda: setattr(self.service, 'submit', lambda h: h.send_json(401, {'key': API_KEY})),
            lambda: setattr(self.service, 'submit', lambda h: h.send_json(202, {'job_id': API_KEY + '/x'})),
            lambda: setattr(self.service, 'polls', [{'status': 'failed', 'error': {'code': 'x', 'message': 'm'}}]),
            lambda: setattr(self.service, 'video', lambda h: h.send_video(b'\x00' * 3000)),
        ]
        for configure in scenarios:
            with self.subTest(scenario=scenarios.index(configure)):
                self.service.submit = lambda h: h.send_json(202, {'job_id': 'remote-1'})
                self.service.polls, self.service.video = [], (lambda h: h.send_video(VIDEO))
                configure()
                with self.assertLogs('ai_service.client.remote', level='DEBUG') as logs, \
                        self.assertRaises(AIServiceError) as ctx:
                    logging.getLogger('ai_service.client.remote').debug('start')
                    self.generate()
                self.assertNotIn(API_KEY, '\n'.join(logs.output))
                self.assertNotIn(API_KEY, ctx.exception.message)


class RemoteWorkerIntegrationTests(RemoteTestMixin, TempMediaTestCase):
    """process_job with the real RemoteClient: failures leave the job and media consistent."""

    def processing_job(self, **fields):
        job = self.make_job(**fields)
        job.transition_to(S.PROCESSING)
        return job

    def media_files(self):
        return sorted(p.relative_to(self.media_root).as_posix() for p in self.media_root.rglob('*') if p.is_file())

    def run_job(self, job, **options):
        with self.assertLogs('apps.generator.worker', level='DEBUG'), \
                self.assertLogs('ai_service.client.remote', level='DEBUG'):
            logging.getLogger('apps.generator.worker').debug('start')
            logging.getLogger('ai_service.client.remote').debug('start')
            process_job(job, client=self.remote_client(**options))
        job.refresh_from_db()
        return job

    def test_success_records_the_video_and_remote_id(self):
        job = self.run_job(self.processing_job())
        self.assertEqual((job.status, job.remote_job_id), (S.COMPLETED, 'remote-1'))
        self.assertEqual((self.media_root / job.output_file.name).read_bytes(), VIDEO)

    def test_failures_leave_no_output_and_keep_the_input(self):
        scenarios = {
            'output_too_large': (lambda s: setattr(s, 'video', lambda h: h.send_video(VIDEO * 20, length=None)),
                                 {'max_video_bytes': 10_000}),
            'invalid_output': (lambda s: setattr(s, 'video', lambda h: h.send_video(b'\x00' * 3000)), {}),
            'download_failed': (lambda s: setattr(s, 'video', lambda h: h.send_video(VIDEO, length=len(VIDEO) + 10)), {}),
            'service_timeout': (lambda s: setattr(s, 'polls', [{'status': 'processing'}] * 1000),
                                {'max_wait': 1, 'poll_interval': 0.2}),
            'service_unavailable': (lambda s: setattr(s, 'submit', lambda h: time.sleep(2)), {'request_timeout': 0.5}),
            'invalid_response': (lambda s: setattr(s, 'submit', lambda h: h.send_json(202, {'job_id': '../x'})), {}),
        }
        for code, (configure, options) in scenarios.items():
            with self.subTest(code=code):
                self.service.submit = lambda h: h.send_json(202, {'job_id': 'remote-1'})
                self.service.polls, self.service.video = [], (lambda h: h.send_video(VIDEO))
                configure(self.service)
                job = self.processing_job(mode=GenerationJob.Mode.IMAGE_TO_VIDEO)
                job.input_file.save(f'in-{code}.png', ContentFile(PNG), save=True)
                before = self.media_files()
                job = self.run_job(job, **options)
                self.assertEqual((job.status, job.error_code), (S.FAILED, code))
                self.assertFalse(job.output_file)
                self.assertEqual(self.media_files(), before)  # no stored or partial video; the image is kept
                self.assertTrue((self.media_root / job.input_file.name).exists())
                if code == 'invalid_response':
                    self.assertEqual(job.remote_job_id, '')  # an invalid ID is never stored
                self.assertNotIn(API_KEY, job.error_message)

    def test_invalid_service_error_codes_fail_the_job_at_once(self):
        # Before 3.5.10 a code with a trailing newline was passed through: 'oom\n' was
        # stored as is, and a 50-character code plus newline was cut to fit
        # error_code (max 50). Only codes that fit the column exactly are passed now,
        # so the result write can never fail on the code and the job never stays
        # `processing` because of it.
        for code in ('a' * 50 + '\n', 'oom\n', 'a' * 51, 'Bad Code'):
            with self.subTest(code=code):
                self.service.polls = [{'status': 'failed', 'error': {'code': code, 'message': 'secret detail'}}]
                job = self.run_job(self.processing_job())
                self.assertEqual((job.status, job.error_code), (S.FAILED, 'generation_failed'))
                self.assertEqual(job.error_message, 'The model could not generate this video.')
                self.assertIsNotNone(job.completed_at)
        self.service.polls = [{'status': 'failed', 'error': {'code': 'a' * 50, 'message': 'secret detail'}}]
        job = self.run_job(self.processing_job())
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'a' * 50))  # a valid code still passes through


@override_settings(WORKER_HEARTBEAT_SECONDS=0.05, WORKER_STALE_AFTER_SECONDS=0.4)
class RemoteDeadlineAndLeaseTests(RemoteTestMixin, TempMediaTransactionTestCase):
    def test_lease_stays_alive_until_the_deadline_then_the_job_fails_with_service_timeout(self):
        # The remote job takes longer than the stale limit; recovery runs during every
        # poll, but the heartbeat keeps the lease alive until the client's own deadline.
        recoveries = []

        def poll_and_recover(handler):
            try:
                recoveries.append(recover_interrupted_jobs())
            finally:
                connection.close()
            handler.send_json(200, {'status': 'processing'})

        self.service.polls = [poll_and_recover] * 1000
        job = self.make_job()
        job.transition_to(S.PROCESSING)
        started = time.monotonic()
        with self.assertLogs('ai_service.client.remote', level='WARNING'), \
                self.assertLogs('apps.generator.worker', level='WARNING'):
            process_job(job, client=self.remote_client(max_wait=1.5, poll_interval=0.1))
        elapsed = time.monotonic() - started
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (S.FAILED, 'service_timeout'))
        self.assertGreater(elapsed, 1.2)  # well past the 0.4 s stale limit
        self.assertLess(elapsed, 3)
        self.assertGreater(len(recoveries), 3)
        self.assertEqual(sum(recoveries), 0)  # never treated as abandoned
        self.assertLess(job.completed_at - job.started_at, timedelta(seconds=3))


class RemoteSettingsTests(SimpleTestCase):
    def test_defaults(self):
        # A developer's .env may tune these; then the configured value must be in effect instead.
        import os
        from django.conf import settings
        defaults = {'AI_SERVICE_TIMEOUT': 30, 'AI_SERVICE_POLL_INTERVAL': 10, 'AI_SERVICE_MAX_WAIT': 1800,
                    'AI_SERVICE_MAX_VIDEO_BYTES': 100 * 1024 * 1024}
        for name, default in defaults.items():
            with self.subTest(name):
                configured = os.environ.get(name, '').strip()
                self.assertEqual(getattr(settings, name), int(configured) if configured else default)
        self.assertEqual(remote.DEFAULT_MAX_VIDEO_BYTES, defaults['AI_SERVICE_MAX_VIDEO_BYTES'])


class DeadlineTimeoutClassificationTests(SimpleTestCase):
    """When the deadline is closer than one request timeout, the socket timeout is set to
    the time left. Socket timeouts can fire a little before time.monotonic() reaches the
    deadline (seen locally: about 1 in 10, by up to 2 ms), which was then reported as
    `service_unavailable` ("check that Colab is running") instead of `service_timeout`.
    These fakes time out 2 ms early on purpose, so the result no longer depends on luck.
    No socket is opened."""

    EARLY = 0.002

    def early_timeout(self, timeout):
        time.sleep(max(0, timeout - self.EARLY))
        raise TimeoutError('timed out')

    def run_generation(self, client, urlopen):
        out_dir = Path(tempfile.mkdtemp(prefix='geetha-test-remote-'))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        with mock.patch.object(remote.urllib.request, 'urlopen', side_effect=urlopen), \
                self.assertLogs('ai_service.client.remote', level='WARNING'), \
                self.assertRaises(AIServiceError) as ctx:
            client.generate(text_request(), out_dir / 'job.mp4')
        self.assertFalse((out_dir / 'job.mp4').exists())
        return ctx.exception

    def test_capped_connect_timeout_that_fires_early_is_the_deadline(self):
        client = RemoteClient('https://ai.example.invalid', API_KEY, request_timeout=30, max_wait=0.3)
        exc = self.run_generation(client, lambda request, timeout: self.early_timeout(timeout))
        self.assertEqual((exc.code, exc.message), ('service_timeout', remote.TIMEOUT_MESSAGE))

    def test_capped_read_timeout_that_fires_early_is_the_deadline(self):
        class Reply:
            def __init__(reply, timeout):
                reply.timeout = timeout

            def __enter__(reply):
                return reply

            def __exit__(reply, *exc_info):
                return False

            def read(reply, size):
                self.early_timeout(reply.timeout)

        client = RemoteClient('https://ai.example.invalid', API_KEY, request_timeout=30, max_wait=0.3)
        exc = self.run_generation(client, lambda request, timeout: Reply(timeout))
        self.assertEqual(exc.code, 'service_timeout')

    def test_ordinary_request_timeout_is_still_service_unavailable(self):
        # The per-request timeout (0.2 s) is far shorter than the deadline: not the deadline.
        client = RemoteClient('https://ai.example.invalid', API_KEY, request_timeout=0.2, max_wait=30)
        exc = self.run_generation(client, lambda request, timeout: self.early_timeout(timeout))
        self.assertEqual(exc.code, 'service_unavailable')
