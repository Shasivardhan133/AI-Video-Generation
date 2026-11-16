"""Server-side file names.

Generated videos are offered as 'geetha-<job id>.mp4', or 'geetha-mock-<job id>.mp4'
when the video is the mock client's test pattern (see video_filename). The API
sends this one name both in Content-Disposition and as `result.download_filename`,
so a download is named the same whether the browser uses the header or the
frontend's `download` attribute.

Uploaded images are stored as '<keyword>-<uuid4 hex>.<extension>'.

The keyword is a short, readable hint taken from the prompt (for example
'white-cat' for 'A white cat walking in a garden'). Only ASCII letters and
digits survive, so the name can never contain path separators, control or
invisible characters, or punctuation. The original upload filename is never
used, the extension comes from the validated image format, and the UUID makes
every name unique even for identical prompts.
"""

import re
import unicodedata
import uuid

FALLBACK_KEYWORD = 'image'
MAX_KEYWORD_WORDS = 2
MAX_KEYWORD_LENGTH = 24
STOP_WORDS = frozenset(
    'a an the and or of in on at to for with from by into onto over under is are be this that these those '
    'it its my your their our make makes create show'.split()
)


def _ascii_words(text):
    # Accents are dropped ('Café' -> 'Cafe'); any other non-ASCII character
    # (other scripts, emoji, invisible or direction-control characters) separates words.
    folded = ''.join(
        '' if unicodedata.category(char).startswith('M') else (char if char.isascii() else ' ')
        for char in unicodedata.normalize('NFKD', text)
    )
    return re.findall(r'[a-z0-9]+', folded.lower())


def prompt_keyword(prompt):
    words = [word for word in _ascii_words(prompt or '') if word not in STOP_WORDS]
    keyword = words[0][:MAX_KEYWORD_LENGTH] if words else ''
    for word in words[1:MAX_KEYWORD_WORDS]:
        if len(keyword) + 1 + len(word) > MAX_KEYWORD_LENGTH:
            break
        keyword = f'{keyword}-{word}'
    return keyword or FALLBACK_KEYWORD


def upload_filename(prompt, extension):
    return f'{prompt_keyword(prompt)}-{uuid.uuid4().hex}.{extension}'


def is_mock_video(job):
    # The mock client tags its jobs 'mock-<id>'; its output is a test pattern, not AI video.
    return job.remote_job_id.startswith('mock-')


def video_filename(job):
    # Only the job's UUID varies, so the name is always ASCII and safe in a header.
    return f'geetha-{"mock-" if is_mock_video(job) else ""}{job.id}.mp4'
