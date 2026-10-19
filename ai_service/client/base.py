from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


class AIServiceError(Exception):
    """A failure that is safe to show to users.

    `code` is a stable category for diagnostics; `message` is user-facing.
    `retryable` marks transient problems such as timeouts.
    """

    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class GenerationRequest:
    job_id: str
    mode: str
    prompt: str
    wan_task: str
    width: int
    height: int
    frame_count: int
    fps: int
    seed: Optional[int] = None
    enhance_prompt: bool = False
    sample_options: Optional[dict] = None
    image_path: Optional[Path] = None

    def payload(self):
        """The JSON body sent to the service (the image travels separately)."""
        return {
            'job_id': self.job_id,
            'mode': self.mode,
            'prompt': self.prompt,
            'wan_task': self.wan_task,
            'width': self.width,
            'height': self.height,
            'frame_count': self.frame_count,
            'fps': self.fps,
            'seed': self.seed,
            'enhance_prompt': self.enhance_prompt,
            'sample_options': self.sample_options or {},
        }


class BaseClient:
    def generate(
        self,
        request: GenerationRequest,
        output_path: Path,
        on_submitted: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Run a generation and write a verified MP4 to `output_path`.

        Raises AIServiceError on any failure. `on_submitted` receives the
        service's own job ID so it can be stored for diagnostics.
        """
        raise NotImplementedError


MP4_MIN_BYTES = 1024


def verify_mp4(path: Path):
    """Reject empty or non-MP4 output instead of reporting a fake success."""
    if not path.exists() or path.stat().st_size < MP4_MIN_BYTES:
        raise AIServiceError('invalid_output', 'The AI service returned an empty video.')
    with path.open('rb') as handle:
        head = handle.read(12)
    if head[4:8] != b'ftyp':
        raise AIServiceError('invalid_output', 'The AI service returned a file that is not an MP4 video.')
