from django.contrib import admin

from .models import GenerationJob


@admin.register(GenerationJob)
class GenerationJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'mode', 'status', 'user', 'created_at', 'completed_at')
    list_filter = ('mode', 'status')
    search_fields = ('id', 'prompt_text', 'remote_job_id', 'request_id')
    readonly_fields = ('id', 'created_at', 'started_at', 'completed_at', 'updated_at')
