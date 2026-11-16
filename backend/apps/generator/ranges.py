"""HTTP byte ranges (RFC 9110, section 14) for streaming generated videos.

Only a single range in the `bytes` unit is supported, which is what browsers
send for <video> playback and seeking:

    bytes=0-99     bytes 0 to 99 (inclusive)
    bytes=100-     from byte 100 to the end
    bytes=-500     the last 500 bytes

Anything else (another unit, several ranges, a malformed value) is ignored and
the whole file is sent with 200, as RFC 9110 allows. A well-formed range that
starts at or beyond the end of the file is unsatisfiable (416).
"""

import re

# ASCII digits only ([0-9], not \d, which also matches other scripts' digits), no
# signs or spaces inside numbers; 19 digits is far beyond any file size.
SINGLE_RANGE = re.compile(r'^bytes=(?P<start>[0-9]{0,19})-(?P<end>[0-9]{0,19})$', re.IGNORECASE)
CHUNK_SIZE = 64 * 1024


class RangeNotSatisfiable(Exception):
    """The range is well formed but selects no bytes of the file."""


def parse_range(header, size):
    """Return (start, end) inclusive for a satisfiable single range, or None to send
    the whole file. Raise RangeNotSatisfiable for a valid but unsatisfiable range."""
    if not header:
        return None
    match = SINGLE_RANGE.match(header.strip())
    if not match:
        return None  # another unit, several ranges, or malformed: ignore it
    start, end = match['start'], match['end']
    if not start and not end:
        return None  # 'bytes=-' is malformed
    if not start:  # suffix range: the last N bytes
        length = int(end)
        if length == 0 or size == 0:
            raise RangeNotSatisfiable
        return max(size - length, 0), size - 1
    first = int(start)
    last = int(end) if end else size - 1
    if end and last < first:
        return None  # invalid range: ignore it
    if first >= size:
        raise RangeNotSatisfiable
    return first, min(last, size - 1)


class FileRange:
    """Streams `length` bytes of an open file from `start`, in bounded chunks.

    StreamingHttpResponse calls close() when the response is closed, whether or
    not the body was read (a HEAD request, or a client that disconnects before
    the first chunk), so the file is always released. A generator's `finally`
    would not run if the generator was never started.
    """

    def __init__(self, handle, start, length, chunk_size=CHUNK_SIZE):
        self.handle, self.start, self.length, self.chunk_size = handle, start, length, chunk_size

    def __iter__(self):
        try:
            self.handle.seek(self.start)
            remaining = self.length
            while remaining > 0:
                chunk = self.handle.read(min(self.chunk_size, remaining))
                if not chunk:
                    break  # the file shrank; send what exists
                remaining -= len(chunk)
                yield chunk
        finally:
            self.close()

    def close(self):
        self.handle.close()
