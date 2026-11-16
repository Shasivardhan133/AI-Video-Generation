import uuid

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .profiles import IMAGE_TO_VIDEO, TEXT_TO_VIDEO


class JobTransitionError(ValueError):
    """A status change was refused; the stored job was left as it is."""


class InvalidJobTransition(JobTransitionError):
    """The lifecycle does not allow this change (for example completed -> processing)."""


class StaleJobState(JobTransitionError):
    """The stored job changed (or was deleted) after this instance was loaded."""


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

    # Status safeguards. Every status change must start from the status that is
    # stored at that moment, never from a stale copy in memory:
    # - transition_to() is one conditional UPDATE (WHERE status = <expected>),
    #   like the worker's, recovery's, expiry's and cancellation's own updates.
    # - save() of an existing row that writes `status` first locks the row
    #   (SELECT ... FOR UPDATE) and refuses to write if the stored status is not
    #   the one this instance last read or wrote, if the row is gone, or if the
    #   change is not in TRANSITIONS.
    # So a stale instance can never revive a cancelled, recovered, completed or
    # failed job, nor undo another operation's result.

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._persisted_status = instance.__dict__.get('status')
        return instance

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        if 'status' in self.__dict__:
            self._persisted_status = self.status

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        writes_status = update_fields is None or 'status' in update_fields
        if self._state.adding or not writes_status:
            super().save(*args, **kwargs)
        else:
            with transaction.atomic(using=kwargs.get('using') or self._state.db):
                stored = (type(self)._base_manager.using(kwargs.get('using') or self._state.db)
                          .select_for_update().filter(pk=self.pk).values_list('status', flat=True).first())
                if stored is None:
                    raise StaleJobState(f'Job {self.pk} no longer exists; not saved')
                loaded = getattr(self, '_persisted_status', stored)
                if stored != loaded:
                    raise StaleJobState(f'Job {self.pk} is now {stored}, not {loaded}; not saved')
                if self.status != stored and self.status not in self.TRANSITIONS[stored]:
                    raise InvalidJobTransition(f'Invalid job transition {stored} -> {self.status}')
                super().save(*args, **kwargs)
        self._persisted_status = self.status

    def transition_to(self, new_status, **fields):
        """Move to `new_status`, set timestamps, and store the given fields.

        One conditional UPDATE: it applies only if the stored status is still
        this instance's status. Raises InvalidJobTransition for a change the
        lifecycle forbids, and StaleJobState if the stored job has moved on (or
        is gone); the instance is then refreshed, and nothing is written.
        """
        expected = self.status
        if new_status not in self.TRANSITIONS[expected]:
            raise InvalidJobTransition(f'Invalid job transition {expected} -> {new_status}')
        now = timezone.now()
        values = {'status': new_status, 'updated_at': now, **fields}
        if new_status == self.Status.PROCESSING:
            values['started_at'] = now
        if new_status in (self.Status.COMPLETED, self.Status.FAILED):
            values['completed_at'] = now
        updated = type(self)._base_manager.filter(pk=self.pk, status=expected).update(**values)
        if not updated:
            try:
                self.refresh_from_db()
            except type(self).DoesNotExist:
                raise StaleJobState(f'Job {self.pk} no longer exists') from None
            raise StaleJobState(f'Job {self.pk} is {self.status}, not {expected}; {new_status} not applied')
        for name, value in values.items():
            setattr(self, name, value)
        self._persisted_status = new_status

    def fail(self, code, message):
        self.transition_to(self.Status.FAILED, error_code=code, error_message=message[:500])
