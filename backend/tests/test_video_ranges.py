"""C3: byte-range (206/416) support on the video endpoint, for <video> seeking."""

import io
from unittest import mock

from django.contrib.auth import get_user_model
from django.http import StreamingHttpResponse
from django.test import Client, SimpleTestCase

from apps.generator.models import GenerationJob
from apps.generator.ranges import CHUNK_SIZE, FileRange, RangeNotSatisfiable, parse_range

from .base import TempMediaTestCase

S = GenerationJob.Status
# Distinct bytes at every offset, so a wrong slice can never look right.
VIDEO = b'\x00\x00\x00\x18ftypisom' + bytes(i % 251 for i in range(5000))
SIZE = len(VIDEO)  # 5012


def video_url(job):
    return f'/api/v1/generations/{job.id}/video/'


class ParseRangeTests(SimpleTestCase):
    def test_no_header_means_whole_file(self):
        for header in (None, ''):
            self.assertIsNone(parse_range(header, 100))

    def test_start_and_end(self):
        self.assertEqual(parse_range('bytes=0-99', 1000), (0, 99))
        self.assertEqual(parse_range('bytes=10-10', 1000), (10, 10))
        self.assertEqual(parse_range('bytes=900-5000', 1000), (900, 999))  # end clamped to the file
        self.assertEqual(parse_range('BYTES=0-0', 1000), (0, 0))  # the unit is case-insensitive
        self.assertEqual(parse_range('  bytes=1-2  ', 1000), (1, 2))

    def test_open_ended(self):
        self.assertEqual(parse_range('bytes=100-', 1000), (100, 999))
        self.assertEqual(parse_range('bytes=0-', 1000), (0, 999))
        self.assertEqual(parse_range('bytes=999-', 1000), (999, 999))

    def test_suffix(self):
        self.assertEqual(parse_range('bytes=-500', 1000), (500, 999))
        self.assertEqual(parse_range('bytes=-1', 1000), (999, 999))
        self.assertEqual(parse_range('bytes=-5000', 1000), (0, 999))  # longer than the file: all of it

    def test_unsatisfiable(self):
        for header, size in (('bytes=1000-', 1000), ('bytes=1000-2000', 1000), ('bytes=-0', 1000),
                             ('bytes=0-0', 0), ('bytes=0-', 0), ('bytes=-5', 0)):
            with self.subTest(header=header, size=size):
                with self.assertRaises(RangeNotSatisfiable):
                    parse_range(header, size)

    def test_malformed_or_unsupported_ranges_are_ignored(self):
        for header in ('bytes=', 'bytes=-', 'bytes=abc', 'bytes=5-1', 'bytes=0-99,200-299', 'bytes=0-1, 5-6',
                       'items=0-99', 'bytes 0-99', 'bytes= 0-99', 'bytes=+1-2', 'bytes=1.5-2', 'bytes=0x10-',
                       'bytes=１-２', 'bytes=' + '9' * 20 + '-', '0-99'):
            with self.subTest(header=header):
                self.assertIsNone(parse_range(header, 1000))


class FileRangeTests(SimpleTestCase):
    def test_yields_exact_bytes_in_bounded_chunks(self):
        data = bytes(i % 256 for i in range(3 * CHUNK_SIZE + 17))
        handle = io.BytesIO(data)
        chunks = list(FileRange(handle, 5, 2 * CHUNK_SIZE + 3))
        self.assertEqual(b''.join(chunks), data[5:5 + 2 * CHUNK_SIZE + 3])
        self.assertTrue(all(len(chunk) <= CHUNK_SIZE for chunk in chunks))
        self.assertTrue(handle.closed)

    def test_closes_the_file_when_abandoned_early(self):
        handle = io.BytesIO(b'x' * (3 * CHUNK_SIZE))
        body = FileRange(handle, 0, 3 * CHUNK_SIZE)
        next(iter(body))
        body.close()  # what the response does when the client disconnects
        self.assertTrue(handle.closed)

    def test_closes_the_file_when_never_read(self):
        # HEAD, or a client that disconnects before the first chunk.
        handle = io.BytesIO(b'x' * 100)
        response = StreamingHttpResponse(FileRange(handle, 0, 10), status=206)
        response.close()
        self.assertTrue(handle.closed)

    def test_reads_only_what_was_requested(self):
        handle = io.BytesIO(b'y' * (10 * CHUNK_SIZE))
        with mock.patch.object(handle, 'read', wraps=handle.read) as read:
            self.assertEqual(b''.join(FileRange(handle, 4 * CHUNK_SIZE, 10)), b'y' * 10)
        self.assertEqual([c.args for c in read.call_args_list], [(10,)])


class VideoRangeTests(TempMediaTestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.session_key()
        self.job = self.make_completed_job(self.mine, content=VIDEO)
        self.url = video_url(self.job)

    def get(self, url=None, range_header=None, client=None, **params):
        headers = {'Range': range_header} if range_header is not None else {}
        response = (client or self.client).get(url or self.url, params, headers=headers)
        # Reading to the end lets the test client close the file (response.close()
        # would also close the test database connection).
        response.body = b''.join(response.streaming_content) if response.streaming else response.content
        return response

    def assertCommonHeaders(self, response):
        self.assertEqual(response['Accept-Ranges'], 'bytes')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('Cookie', response['Vary'])

    def assertPartial(self, response, start, end, disposition='inline'):
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response['Content-Type'], 'video/mp4')
        self.assertEqual(response['Content-Range'], f'bytes {start}-{end}/{SIZE}')
        self.assertEqual(response['Content-Length'], str(end - start + 1))
        self.assertEqual(response['Content-Disposition'], f'{disposition}; filename="geetha-mock-{self.job.id}.mp4"')
        self.assertEqual(response.body, VIDEO[start:end + 1])
        self.assertCommonHeaders(response)

    def assertWhole(self, response, disposition='inline'):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'video/mp4')
        self.assertEqual(response['Content-Length'], str(SIZE))
        self.assertEqual(response['Content-Disposition'], f'{disposition}; filename="geetha-mock-{self.job.id}.mp4"')
        self.assertNotIn('Content-Range', response)
        self.assertEqual(response.body, VIDEO)
        self.assertCommonHeaders(response)

    def assertUnsatisfiable(self, response, size=SIZE):
        error = self.assertApiError(response, 416, 'range_not_satisfiable')
        self.assertEqual(error['message'], 'The requested range is not satisfiable.')
        self.assertEqual(response['Content-Range'], f'bytes */{size}')
        self.assertNotIn(b'ftyp', response.content)
        self.assertCommonHeaders(response)

    # 1. Whole file
    def test_without_range_the_whole_file_is_sent(self):
        self.assertWhole(self.get())

    # 2-3. Satisfiable ranges, exact bytes
    def test_start_and_end_range(self):
        self.assertPartial(self.get(range_header='bytes=0-99'), 0, 99)
        self.assertPartial(self.get(range_header='bytes=1234-2345'), 1234, 2345)
        self.assertPartial(self.get(range_header='bytes=7-7'), 7, 7)
        self.assertPartial(self.get(range_header=f'bytes=5000-{SIZE + 100}'), 5000, SIZE - 1)

    def test_open_ended_and_suffix_ranges(self):
        self.assertPartial(self.get(range_header='bytes=100-'), 100, SIZE - 1)
        self.assertPartial(self.get(range_header='bytes=0-'), 0, SIZE - 1)  # what browsers send first
        self.assertPartial(self.get(range_header='bytes=-500'), SIZE - 500, SIZE - 1)
        self.assertPartial(self.get(range_header='bytes=-99999'), 0, SIZE - 1)

    def test_range_spanning_several_chunks(self):
        big = bytes(i % 253 for i in range(3 * CHUNK_SIZE + 11))
        job = self.make_completed_job(self.mine, content=big)
        response = self.get(video_url(job), range_header=f'bytes=10-{2 * CHUNK_SIZE + 20}')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.body, big[10:2 * CHUNK_SIZE + 21])
        self.assertEqual(response['Content-Range'], f'bytes 10-{2 * CHUNK_SIZE + 20}/{len(big)}')

    # 4. Invalid and unsatisfiable ranges
    def test_unsatisfiable_ranges_get_416(self):
        for header in (f'bytes={SIZE}-', f'bytes={SIZE}-{SIZE + 10}', 'bytes=99999-', 'bytes=-0'):
            with self.subTest(header):
                self.assertUnsatisfiable(self.get(range_header=header))

    def test_malformed_multiple_and_other_unit_ranges_get_the_whole_file(self):
        for header in ('bytes=abc', 'bytes=5-1', 'bytes=', 'items=0-10', 'bytes=0-9,20-29', 'bytes=0-1, 3-4'):
            with self.subTest(header):
                self.assertWhole(self.get(range_header=header))

    def test_if_range_gets_the_whole_file(self):
        response = self.client.get(self.url, headers={'Range': 'bytes=0-9', 'If-Range': '"anything"'})
        response.body = b''.join(response.streaming_content)
        self.assertWhole(response)

    # 5. Empty file
    def test_empty_file(self):
        job = self.make_completed_job(self.mine, content=b'')
        response = self.get(video_url(job))
        self.assertEqual((response.status_code, response['Content-Length'], response.body), (200, '0', b''))
        self.assertEqual(response['Accept-Ranges'], 'bytes')
        for header in ('bytes=0-0', 'bytes=0-', 'bytes=-5'):
            with self.subTest(header):
                self.assertUnsatisfiable(self.get(video_url(job), range_header=header), size=0)

    # 6. HEAD
    def test_head_with_and_without_range(self):
        response = self.client.head(self.url, headers={'Range': 'bytes=0-99'})
        self.assertEqual((response.status_code, response['Content-Range'], response['Content-Length']),
                         (206, f'bytes 0-99/{SIZE}', '100'))
        self.assertEqual(b''.join(response.streaming_content), b'')
        response = self.client.head(self.url)
        self.assertEqual((response.status_code, response['Content-Length'], response['Accept-Ranges']),
                         (200, str(SIZE), 'bytes'))
        b''.join(response.streaming_content)

    # 7. Inline playback and download
    def test_download_keeps_working_with_and_without_range(self):
        self.assertWhole(self.get(download='1'), disposition='attachment')
        self.assertPartial(self.get(range_header='bytes=0-99', download='1'), 0, 99, disposition='attachment')
        self.assertPartial(self.get(range_header='bytes=0-99', download='true'), 0, 99)  # only '1' downloads

    # 8. Ownership and unavailable videos
    def test_other_owners_get_404_with_or_without_range(self):
        stranger = Client()
        stranger.get('/api/v1/csrf/')
        user = get_user_model().objects.create_user('alice', password='unused-test-password')
        signed_in = Client()
        signed_in.force_login(user)
        for client in (stranger, signed_in):
            for header in (None, 'bytes=0-99', 'bytes=99999-'):
                with self.subTest(client=client, header=header):
                    response = self.get(range_header=header, client=client)
                    self.assertApiError(response, 404, 'not_found')
                    self.assertNotIn('Content-Range', response)

    def test_unavailable_videos_get_404_with_range(self):
        unfinished = [self.make_job(self.mine, status=status) for status in (S.QUEUED, S.PROCESSING, S.FAILED)]
        missing = self.make_completed_job(self.mine)
        (self.media_root / missing.output_file.name).unlink()
        for job in (*unfinished, missing):
            with self.subTest(status=job.status):
                response = self.get(video_url(job), range_header='bytes=0-99')
                error = self.assertApiError(response, 404, 'not_found')
                self.assertNotIn('Content-Range', response)
                self.assertNotIn(str(self.media_root), error['message'])
        unknown = '/api/v1/generations/00000000-0000-4000-8000-000000000000/video/'
        self.assertApiError(self.get(unknown, range_header='bytes=0-99'), 404, 'not_found')

    def test_file_removed_after_the_existence_check_is_404(self):
        with mock.patch('django.db.models.fields.files.FieldFile.open', side_effect=FileNotFoundError('gone')):
            self.assertApiError(self.get(range_header='bytes=0-99'), 404, 'not_found')

    def test_never_exposes_file_paths(self):
        for header in (None, 'bytes=0-99', 'bytes=99999-'):
            response = self.get(range_header=header)
            for value in response.headers.values():
                self.assertNotIn(str(self.media_root), value)
                self.assertNotIn('outputs/', value)

    # 9. Unchanged API behaviour around it
    def test_file_is_released_after_a_range_response(self):
        self.get(range_header='bytes=0-99')
        (self.media_root / self.job.output_file.name).unlink()  # fails on Windows if still open

    def test_file_is_released_after_head_requests(self):
        for headers in ({'Range': 'bytes=0-99'}, {}):
            job = self.make_completed_job(self.mine, content=VIDEO)
            with self.subTest(headers=headers):
                response = self.client.head(video_url(job), headers=headers)
                b''.join(response.streaming_content)  # empty for HEAD; lets the client close the response
                (self.media_root / job.output_file.name).unlink()  # fails on Windows if still open

    def test_writes_to_the_video_url_are_still_rejected(self):
        browser = Client(enforce_csrf_checks=True)
        self.assertApiError(browser.post(self.url, headers={'Range': 'bytes=0-9'}), 403, 'forbidden')
        self.assertApiError(self.client.post(self.url, headers={'Range': 'bytes=0-9'}), 405, 'method_not_allowed')



class VideoRangeRealLimitTests(TempMediaTestCase):
    relax_generation_limits = False  # the real 5/minute and 30/hour generation limits

    def test_many_range_requests_are_not_rate_limited(self):
        job = self.make_completed_job(self.session_key(), content=VIDEO)
        for start in range(0, 40 * 100, 100):  # seeking a video makes many requests
            response = self.client.get(video_url(job), headers={'Range': f'bytes={start}-{start + 99}'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(b''.join(response.streaming_content), VIDEO[start:start + 100])
