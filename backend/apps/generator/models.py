import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .profiles import IMAGE_TO_VIDEO, TEXT_TO_VIDEO


class GenerationJob(models.Model):
    """One text-to-video or image-to-video request and its lifecycle.

    Only file references are stored here; image and video bytes live in
    MEDIA_ROOT (or, later, another storage backend).
    """

    class Mode(models.TextChoices):
        TEXT_TO_VIDEO = TEXT_TO_VIDEO, 'Text to video'
        IMAGE_TO_VIDEO = IMAGE_TO_VIDEO, 'Image to video'

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    # Allowed lifecycle transitions; anything else is a programming error.
    TRANSITIONS = {
        Status.QUEUED: {Status.PROCESSING, Status.FAILED},
        Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
        Status.COMPLETED: set(),
        Status.FAILED: set(),
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='generation_jobs',
    )
    # Anonymous demo users own jobs through their browser session.
    session_key = models.CharField(max_length=40, blank=True, db_index=True)
    mode = models.CharField(max_length=20, choices=Mode.choices)
    prompt_text = models.TextField(blank=True)
    settings = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    input_file = models.FileField(upload_to='inputs/%Y/%m/', blank=True)
    output_file = models.FileField(upload_to='outputs/%Y/%m/', blank=True)
    remote_job_id = models.CharField(max_length=100, blank=True)
    error_code = models.CharField(max_length=50, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'generation_jobs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['session_key', 'created_at']),
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f'{self.mode} {self.id} ({self.status})'

    @property
    def video_available(self):
        return self.status == self.Status.COMPLETED and bool(self.output_file)

    def transition_to(self, new_status, **fields):
        """Move to `new_status`, set timestamps, and save the changed fields."""
        if new_status not in self.TRANSITIONS[self.status]:
            raise ValueError(f'Invalid job transition {self.status} -> {new_status}')
        self.status = new_status
        now = timezone.now()
        if new_status == self.Status.PROCESSING:
            self.started_at = now
        if new_status in (self.Status.COMPLETED, self.Status.FAILED):
            self.completed_at = now
        for name, value in fields.items():
            setattr(self, name, value)
        update_fields = ['status', 'started_at', 'completed_at', 'updated_at', *fields]
        self.save(update_fields=update_fields)

    def fail(self, code, message):
        self.transition_to(self.Status.FAILED, error_code=code, error_message=message[:500])
