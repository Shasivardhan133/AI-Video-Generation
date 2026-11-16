import json
import unicodedata
from collections.abc import Mapping

from django.http import QueryDict
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail

from .filenames import is_mock_video, upload_filename, video_filename
from .models import GenerationJob
from .profiles import DEFAULT_PROFILE, IMAGE_TO_VIDEO, STYLES, TEXT_TO_VIDEO, get_profile
from .validators import inspect_image

MAX_PROMPT_LENGTH = 1000
MAX_SEED = 2**31 - 1
UNKNOWN_FIELD_MESSAGE = 'Unknown field.'


def reject_unknown_fields(data, allowed):
    """400 `unknown_field` for every key that is not part of the API (DRF would
    otherwise ignore it silently, so a misspelt setting would look accepted)."""
    unknown = sorted(str(name) for name in set(data) - set(allowed))
    if unknown:
        raise serializers.ValidationError(
            {name: [ErrorDetail(UNKNOWN_FIELD_MESSAGE, code='unknown_field')] for name in unknown}
        )


# Settings arrive as parsed JSON (a JSON body, or the JSON string of a form
# field), so their values must already have the right JSON type: DRF's fields
# would otherwise convert "832", 832.0, "yes" or 1.
class StrictIntegerField(serializers.IntegerField):
    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, int):
            self.fail('invalid')
        return super().to_internal_value(data)


class StrictBooleanField(serializers.BooleanField):
    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail('invalid')
        return data


class StrictCharField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail('invalid')
        return super().to_internal_value(data)


class GenerationSettingsSerializer(serializers.Serializer):
    """Validates settings against the selected model profile and fills defaults."""

    model_profile = StrictCharField(required=False)
    width = StrictIntegerField(required=False)
    height = StrictIntegerField(required=False)
    frame_count = StrictIntegerField(required=False)
    fps = StrictIntegerField(required=False)
    seed = StrictIntegerField(required=False, allow_null=True, min_value=0, max_value=MAX_SEED)
    style = serializers.ChoiceField(choices=STYLES, required=False, allow_null=True)
    enhance_prompt = StrictBooleanField(required=False, default=False)

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            reject_unknown_fields(data, self.fields)
        return super().to_internal_value(data)

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
    """Settings: a JSON object in a JSON body, or the JSON text of an object in a
    form or multipart body (whose fields can only be text). An empty form field
    is the same as leaving settings out."""

    def to_internal_value(self, data):
        form_body = isinstance(getattr(self.parent, 'initial_data', None), QueryDict)
        if isinstance(data, str):
            if not form_body:
                raise serializers.ValidationError('Settings must be a JSON object.', code='invalid')
            if not data.strip():
                return {}
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, RecursionError):  # RecursionError: absurdly deep nesting
                raise serializers.ValidationError('Settings must be valid JSON.', code='invalid')
        if not isinstance(data, dict):
            raise serializers.ValidationError('Settings must be a JSON object.', code='invalid')
        return data


class _CreateJobSerializer(serializers.Serializer):
    mode = None
    settings = JSONStringField(required=False, default=dict)
    # Not part of the API, but allowed: Django's CSRF token sent as a form field.
    allowed_extra_fields = frozenset({'csrfmiddlewaretoken'})

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            reject_unknown_fields(data, set(self.fields) | self.allowed_extra_fields)
        return super().to_internal_value(data)

    def validate_settings(self, value):
        serializer = GenerationSettingsSerializer(data=value, context={'mode': self.mode})
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


# Characters that render as nothing: controls, format characters such as the
# zero-width space, separators (spaces), lone combining marks, and fillers.
INVISIBLE_CATEGORIES = frozenset({'Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zs', 'Zl', 'Zp', 'Mn', 'Me'})
INVISIBLE_FILLERS = frozenset('ᅟᅠㅤﾠ⠀')


def has_visible_text(text):
    return any(
        char not in INVISIBLE_FILLERS and unicodedata.category(char) not in INVISIBLE_CATEGORIES
        for char in text
    )


class PromptField(serializers.CharField):
    """A prompt must be a string (numbers and booleans are not converted) and,
    when not blank, must contain visible text. Unicode and emoji are kept as typed."""

    default_error_messages = {'invisible': 'Enter a prompt with visible text.'}

    def __init__(self, **kwargs):
        super().__init__(max_length=MAX_PROMPT_LENGTH, trim_whitespace=True, **kwargs)

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail('invalid')
        value = super().to_internal_value(data)
        if value and not has_visible_text(value):
            raise serializers.ValidationError(self.error_messages['invisible'], code='blank')
        return value


class TextToVideoSerializer(_CreateJobSerializer):
    mode = TEXT_TO_VIDEO
    prompt = PromptField()


class ImageToVideoSerializer(_CreateJobSerializer):
    mode = IMAGE_TO_VIDEO
    # Empty files are rejected by inspect_image with the documented
    # `invalid_image` code, rather than by DRF's generic `empty` check.
    image = serializers.FileField(allow_empty_file=True)
    prompt = PromptField(required=False, allow_blank=True)

    def validate_image(self, upload):
        upload.image_info = inspect_image(upload)
        return upload

    def validate(self, attrs):
        # Stored as '<prompt keyword>-<uuid>.<ext>'; the client's filename is never used.
        image = attrs['image']
        image.name = upload_filename(attrs.get('prompt', ''), image.image_info.extension)
        return attrs


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
            # The same name the video endpoint sends in Content-Disposition.
            'download_filename': video_filename(job),
            'is_mock': is_mock_video(job),
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
