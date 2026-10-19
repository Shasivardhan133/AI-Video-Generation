import json

from rest_framework import serializers

from .models import GenerationJob
from .profiles import DEFAULT_PROFILE, IMAGE_TO_VIDEO, STYLES, TEXT_TO_VIDEO, get_profile
from .validators import validate_image_upload

MAX_PROMPT_LENGTH = 1000
MAX_SEED = 2**31 - 1


class GenerationSettingsSerializer(serializers.Serializer):
    """Validates settings against the selected model profile and fills defaults."""

    model_profile = serializers.CharField(required=False)
    width = serializers.IntegerField(required=False)
    height = serializers.IntegerField(required=False)
    frame_count = serializers.IntegerField(required=False)
    fps = serializers.IntegerField(required=False)
    seed = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=MAX_SEED)
    style = serializers.ChoiceField(choices=STYLES, required=False, allow_null=True)
    enhance_prompt = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        mode = self.context['mode']
        profile_name = attrs.get('model_profile') or DEFAULT_PROFILE[mode]
        profile = get_profile(profile_name)
        if profile is None or profile.mode != mode:
            raise serializers.ValidationError(
                {'model_profile': 'This model profile is not available for this mode.'},
                code='unsupported_setting',
            )

        default_width, default_height = profile.default_size()
        size = (attrs.get('width', default_width), attrs.get('height', default_height))
        if size not in profile.sizes:
            allowed = ', '.join(f'{w}x{h}' for w, h in profile.sizes)
            raise serializers.ValidationError(
                {'width': f'Supported sizes for this profile are {allowed}.'},
                code='unsupported_setting',
            )

        frame_count = attrs.get('frame_count', profile.default_frames)
        if frame_count not in profile.allowed_frame_counts():
            raise serializers.ValidationError(
                {'frame_count': (
                    f'Frame count must be between {profile.min_frames} and '
                    f'{profile.max_frames} and of the form 4n + 1.'
                )},
                code='unsupported_setting',
            )

        fps = attrs.get('fps', profile.fps)
        if fps != profile.fps:
            raise serializers.ValidationError(
                {'fps': f'This profile only supports {profile.fps} fps.'},
                code='unsupported_setting',
            )

        enhance = attrs.get('enhance_prompt', False)
        if enhance and not profile.supports_prompt_enhancement:
            raise serializers.ValidationError(
                {'enhance_prompt': 'Prompt enhancement is not available for this profile.'},
                code='unsupported_setting',
            )

        return {
            'model_profile': profile.name,
            'width': size[0],
            'height': size[1],
            'frame_count': frame_count,
            'fps': fps,
            'seed': attrs.get('seed'),
            'style': attrs.get('style'),
            'enhance_prompt': enhance,
        }


class JSONStringField(serializers.Field):
    """Accepts settings as a JSON object or, in multipart forms, a JSON string."""

    def to_internal_value(self, data):
        if isinstance(data, str):
            try:
                data = json.loads(data) if data.strip() else {}
            except json.JSONDecodeError:
                raise serializers.ValidationError('Settings must be valid JSON.')
        if not isinstance(data, dict):
            raise serializers.ValidationError('Settings must be a JSON object.')
        return data


class _CreateJobSerializer(serializers.Serializer):
    mode = None
    settings = JSONStringField(required=False, default=dict)

    def validate_settings(self, value):
        serializer = GenerationSettingsSerializer(data=value, context={'mode': self.mode})
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class TextToVideoSerializer(_CreateJobSerializer):
    mode = TEXT_TO_VIDEO
    prompt = serializers.CharField(max_length=MAX_PROMPT_LENGTH, trim_whitespace=True)


class ImageToVideoSerializer(_CreateJobSerializer):
    mode = IMAGE_TO_VIDEO
    image = serializers.FileField(validators=[validate_image_upload])
    prompt = serializers.CharField(
        max_length=MAX_PROMPT_LENGTH, required=False, allow_blank=True, trim_whitespace=True,
    )


def job_urls(job, request):
    status_url = f'/api/v1/generations/{job.id}/'
    return {
        'status_url': status_url,
        'video_url': f'{status_url}video/',
    }


class JobSerializer(serializers.ModelSerializer):
    job_id = serializers.UUIDField(source='id')
    progress = serializers.SerializerMethodField()
    result = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()
    prompt = serializers.CharField(source='prompt_text')
    has_input_image = serializers.SerializerMethodField()

    class Meta:
        model = GenerationJob
        fields = [
            'job_id', 'mode', 'status', 'progress', 'prompt', 'settings', 'has_input_image',
            'created_at', 'started_at', 'completed_at', 'result', 'error',
        ]

    def get_progress(self, job):
        # No trustworthy intermediate progress signal exists yet, so never invent one.
        return 100 if job.video_available else None

    def get_result(self, job):
        if not job.video_available:
            return None
        video_url = job_urls(job, self.context.get('request'))['video_url']
        return {
            'preview_url': video_url,
            'download_url': f'{video_url}?download=1',
            # The mock client tags its jobs 'mock-<id>'; its output is a test pattern, not AI video.
            'is_mock': job.remote_job_id.startswith('mock-'),
        }

    def get_error(self, job):
        if job.status != GenerationJob.Status.FAILED:
            return None
        return {'code': job.error_code, 'message': job.error_message, 'request_id': job.request_id}

    def get_has_input_image(self, job):
        return bool(job.input_file)


class JobListSerializer(serializers.ModelSerializer):
    job_id = serializers.UUIDField(source='id')
    prompt_preview = serializers.SerializerMethodField()
    video_available = serializers.BooleanField(read_only=True)

    class Meta:
        model = GenerationJob
        fields = ['job_id', 'mode', 'prompt_preview', 'status', 'created_at', 'completed_at', 'video_available']

    def get_prompt_preview(self, job):
        text = job.prompt_text or ''
        return text if len(text) <= 80 else f'{text[:77]}...'
