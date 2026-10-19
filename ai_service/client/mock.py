"""Local stand-in for the Colab service, used to test the full app workflow.

It does NOT run an AI model. It renders an FFmpeg test pattern with the
requested size, length and frame rate, so the result is obviously synthetic.
"""

import shutil
import subprocess
import time

from .base import AIServiceError, BaseClient, verify_mp4


class MockClient(BaseClient):
    def __init__(self, delay_seconds=3, ffmpeg_path=None):
        self.delay_seconds = delay_seconds
        self.ffmpeg_path = ffmpeg_path or shutil.which('ffmpeg')

    def generate(self, request, output_path, on_submitted=None):
        if on_submitted:
            on_submitted(f'mock-{request.job_id}')
        if not self.ffmpeg_path:
            raise AIServiceError(
                'mock_unavailable',
                'Mock mode needs FFmpeg on PATH (for example: winget install Gyan.FFmpeg).',
            )
        time.sleep(self.delay_seconds)

        duration = request.frame_count / request.fps
        command = [
            self.ffmpeg_path, '-y', '-loglevel', 'error',
            '-f', 'lavfi',
            '-i', f'testsrc2=size={request.width}x{request.height}:rate={request.fps}:duration={duration}',
            '-frames:v', str(request.frame_count),
            '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-movflags', '+faststart',
            str(output_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise AIServiceError('mock_failed', 'The mock video could not be rendered.') from exc
        verify_mp4(output_path)
