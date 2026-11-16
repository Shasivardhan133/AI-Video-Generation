import hashlib

from django.contrib import admin

from .models import GenerationJob


@admin.register(GenerationJob)
class GenerationJobAdmin(admin.ModelAdmin):
    """Read-only inspection of generation jobs.

    Jobs are created only by the API and changed only by the worker, which uses
    status-checked updates. An admin form instead saves every field it shows, so
    a form opened before the worker finished could undo the result (for example
    turn a completed job back into `processing` and drop its video reference).
    The admin therefore cannot add or change jobs; it only shows them.
    Deleting a job is still possible (the worker handles a job deleted mid-run).
    """

    list_display = ('id', 'mode', 'status', 'owner', 'created_at', 'completed_at')
    list_select_related = ('user',)
    list_filter = ('mode', 'status', 'created_at')
    search_fields = ('id', 'prompt_text', 'remote_job_id', 'request_id')
    fieldsets = (
        (None, {'fields': ('id', 'mode', 'status', 'owner', 'prompt_text', 'settings')}),
        ('Files', {'fields': ('input_file_name', 'output_file_name', 'video_available')}),
        ('Errors and tracing', {'fields': ('error_code', 'error_message', 'request_id', 'remote_job_id')}),
        ('Timestamps', {'fields': ('created_at', 'started_at', 'completed_at', 'updated_at')}),
    )
    readonly_fields = tuple(name for _, options in fieldsets for name in options['fields'])

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Owner')
    def owner(self, job):
        if job.user_id:
            return job.user.get_username()
        if not job.session_key:
            return '-'
        # The session key works like a password for a guest's jobs, so only a short,
        # non-reversible fingerprint is shown; jobs from one session share it.
        return f'Guest session {hashlib.sha256(job.session_key.encode()).hexdigest()[:8]}'

    # File references as plain text: media files are never served by URL.
    @admin.display(description='Input image')
    def input_file_name(self, job):
        return job.input_file.name or '-'

    @admin.display(description='Output video')
    def output_file_name(self, job):
        return job.output_file.name or '-'

    @admin.display(description='Video available', boolean=True)
    def video_available(self, job):
        return job.video_available
