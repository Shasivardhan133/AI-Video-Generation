import logging

from django.http import FileResponse, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics, status
from rest_framework.exceptions import APIException, NotFound, ParseError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .active_jobs import ensure_no_active_job, owner_identity, owner_lock
from .exceptions import api_error_response
from .filenames import video_filename
from .media_files import discard_file, store_new_file, stored_file_path
from .models import GenerationJob
from .profiles import DEFAULT_PROFILE, PROFILES
from .ranges import FileRange, RangeNotSatisfiable, parse_range
from .serializers import (
    ImageToVideoSerializer,
    JobListSerializer,
    JobSerializer,
    TextToVideoSerializer,
)
from .throttles import GenerationBurstThrottle, GenerationSustainedThrottle, GenerationThrottled


logger = logging.getLogger(__name__)


def _session_key(request):
    """Anonymous users own jobs through their session; create one if needed."""
    if not request.session.session_key:
        request.session.save()
    return request.session.session_key


def owned_jobs(request):
    if request.user.is_authenticated:
        return GenerationJob.objects.filter(user=request.user)
    # A visitor without a session has not created any jobs, so reads must not
    # create one; the session starts with the first job (see _CreateJobView).
    session_key = request.session.session_key
    if not session_key:
        return GenerationJob.objects.none()
    return GenerationJob.objects.filter(user__isnull=True, session_key=session_key)


class PrivateResponseMixin:
    """For responses that depend on who is asking: a job, a job list or a video
    belongs to one session or user, and a CSRF token to one browser.

    Every response, errors included, is marked `private, no-store` so neither a
    shared cache (proxy, CDN) nor the browser cache keeps a copy, and varies on
    Cookie in case a cache ignores Cache-Control.
    """

    CACHE_CONTROL = 'private, no-store'

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = self.CACHE_CONTROL
        patch_vary_headers(response, ('Cookie',))
        return response


class SafeJSONParser(JSONParser):
    """DRF's JSON parser, except that absurdly deep nesting (which makes Python's
    JSON decoder raise RecursionError) is a 400 like any other malformed JSON."""

    def parse(self, stream, media_type=None, parser_context=None):
        try:
            return super().parse(stream, media_type, parser_context)
        except RecursionError:
            raise ParseError('JSON parse error - the data is nested too deeply.')


INPUT_FILE_FIELD = GenerationJob._meta.get_field('input_file')


def _image_may_be_referenced(name):
    """After a failed INSERT: False only if no job is known to refer to `name`.

    A lost connection can hide a commit that did happen, so the image is
    deleted only when a fresh read confirms that no job refers to it; if that
    read fails too, the image is kept (find_orphaned_media will report it).
    """
    try:
        return GenerationJob.objects.filter(input_file=name).exists()
    except Exception:
        return True


class _CreateJobView(PrivateResponseMixin, APIView):
    serializer_class = None
    # Shared by both generation endpoints; read endpoints are never throttled.
    throttle_classes = [GenerationBurstThrottle, GenerationSustainedThrottle]

    def get_throttles(self):
        # DRF checks throttles before it rejects an unsupported method, so only
        # POST (job creation) may use the allowance; GET, OPTIONS, etc. never do.
        if self.request.method != 'POST':
            return []
        return super().get_throttles()

    def throttled(self, request, wait):
        raise GenerationThrottled(wait)

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        session_key = '' if request.user.is_authenticated else _session_key(request)
        # One active job per owner (see active_jobs). The check and the insert run
        # under the owner's lock, and the image is stored only once the check passed.
        with owner_lock(owner_identity(request, session_key)):
            ensure_no_active_job(owned_jobs(request))
            image = data.get('image')
            input_name = store_new_file(INPUT_FILE_FIELD, None, image.name, image) if image else ''
            try:
                job = GenerationJob.objects.create(
                    user=request.user if request.user.is_authenticated else None,
                    session_key=session_key,
                    mode=self.serializer_class.mode,
                    prompt_text=data.get('prompt', ''),
                    settings=data['settings'],
                    input_file=input_name,
                    request_id=request.request_id,
                )
            except BaseException:
                if input_name and not _image_may_be_referenced(input_name):
                    discard_file(INPUT_FILE_FIELD.storage, input_name, 'image of a job that was not created')
                raise
        return Response(
            {
                'job_id': str(job.id),
                'mode': job.mode,
                'status': job.status,
                'created_at': job.created_at,
                'status_url': f'/api/v1/generations/{job.id}/',
            },
            status=status.HTTP_202_ACCEPTED,
        )


class TextToVideoView(_CreateJobView):
    serializer_class = TextToVideoSerializer
    parser_classes = [SafeJSONParser, FormParser, MultiPartParser]


class ImageToVideoView(_CreateJobView):
    serializer_class = ImageToVideoSerializer
    parser_classes = [MultiPartParser, FormParser]


class JobListView(PrivateResponseMixin, generics.ListAPIView):
    serializer_class = JobListSerializer

    def get_queryset(self):
        jobs = owned_jobs(self.request)
        status_filter = self.request.query_params.get('status')
        if status_filter in GenerationJob.Status.values:
            jobs = jobs.filter(status=status_filter)
        mode_filter = self.request.query_params.get('mode')
        if mode_filter in GenerationJob.Mode.values:
            jobs = jobs.filter(mode=mode_filter)
        return jobs


class JobDetailView(PrivateResponseMixin, generics.RetrieveAPIView):
    serializer_class = JobSerializer
    lookup_url_kwarg = 'job_id'

    def get_queryset(self):
        return owned_jobs(self.request)


CANCELLED_CODE = 'cancelled'
CANCELLED_MESSAGE = 'You cancelled this video.'
NOT_CANCELLABLE_MESSAGES = {
    GenerationJob.Status.PROCESSING: 'This video is already being generated and can no longer be cancelled.',
    GenerationJob.Status.COMPLETED: 'This video has already finished.',
    GenerationJob.Status.FAILED: 'This video has already stopped.',
}


class NotCancellable(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = 'not_cancellable'
    default_detail = 'This video can no longer be cancelled.'


class JobCancelView(PrivateResponseMixin, APIView):
    """POST: cancel one of the caller's own `queued` jobs.

    The job becomes `failed` with error code `cancelled` (no new status, so no
    migration), which also frees the owner's one-active-job allowance. Only
    queued jobs can be cancelled: a processing job's GPU work cannot be stopped,
    and finished jobs keep their outcome. Nothing is deleted: the input image
    stays, and a cancelled job never had an output.

    One conditional UPDATE (WHERE status = 'queued'): a worker claiming the job
    at the same moment holds its row lock, so either the claim or the
    cancellation wins, never both. Cancelling an already cancelled job is
    harmless and returns it unchanged (200). Other states get 409
    `not_cancellable`; someone else's job, like a missing one, gets 404.
    """

    def post(self, request, job_id):
        jobs = owned_jobs(request)
        get_object_or_404(jobs, pk=job_id)
        now = timezone.now()
        cancelled = jobs.filter(pk=job_id, status=GenerationJob.Status.QUEUED).update(
            status=GenerationJob.Status.FAILED,
            error_code=CANCELLED_CODE,
            error_message=CANCELLED_MESSAGE,
            completed_at=now,
            updated_at=now,
        )
        job = jobs.filter(pk=job_id).first()
        if job is None:
            raise NotFound()  # deleted meanwhile
        if cancelled:
            logger.info('Job %s cancelled by its owner (request %s)', job.id, request.request_id)
        elif not (job.status == GenerationJob.Status.FAILED and job.error_code == CANCELLED_CODE):
            raise NotCancellable(NOT_CANCELLABLE_MESSAGES.get(job.status))
        return Response(JobSerializer(job, context={'request': request}).data)


class JobVideoView(PrivateResponseMixin, APIView):
    """Streams a job's MP4, whole (200) or as one byte range (206), for <video> seeking.

    `?download=1` sends it as an attachment, anything else inline for playback;
    both use the name from video_filename(), as `result.download_filename` does.
    """

    def get(self, request, job_id):
        # Jobs that belong to someone else look identical to missing jobs.
        job = get_object_or_404(owned_jobs(request), pk=job_id)
        # Only a regular file inside MEDIA_ROOT is served; a reference that leads
        # elsewhere (traversal, a directory, a link to outside) counts as missing.
        if not job.video_available or stored_file_path(job.output_file) is None:
            raise NotFound('This video is not available yet.')
        try:
            size = job.output_file.size
            handle = job.output_file.open('rb')
        except FileNotFoundError:
            raise NotFound('This video is not available yet.')  # removed since the check above

        try:
            # If-Range needs a validator (ETag/Last-Modified), which these responses
            # do not have, so a conditional range always gets the whole file.
            byte_range = None if 'If-Range' in request.headers else parse_range(request.headers.get('Range'), size)
        except RangeNotSatisfiable:
            handle.close()
            response = api_error_response(request, 416, 'range_not_satisfiable',
                                          'The requested range is not satisfiable.')
            response['Content-Range'] = f'bytes */{size}'
            return self._finish(response)

        download = request.query_params.get('download') == '1'
        filename = video_filename(job)
        if byte_range is None:
            response = FileResponse(handle, as_attachment=download, filename=filename, content_type='video/mp4')
            return self._finish(response)

        start, end = byte_range
        response = StreamingHttpResponse(FileRange(handle, start, end - start + 1), status=206,
                                         content_type='video/mp4')
        response['Content-Length'] = str(end - start + 1)
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
        response['Content-Disposition'] = content_disposition_header(download, filename)
        return self._finish(response)

    def _finish(self, response):
        response['Accept-Ranges'] = 'bytes'
        return response


class ProfileListView(APIView):
    """Lets the frontend show only settings the backend will accept."""

    def get(self, request):
        return Response({
            'results': [
                {
                    'name': profile.name,
                    'mode': profile.mode,
                    # The profile used when a request names none (one per mode).
                    'is_default': DEFAULT_PROFILE[profile.mode] == profile.name,
                    'sizes': [{'width': w, 'height': h} for w, h in profile.sizes],
                    'frame_counts': profile.allowed_frame_counts(),
                    'default_frame_count': profile.default_frames,
                    'fps': profile.fps,
                    'supports_prompt_enhancement': profile.supports_prompt_enhancement,
                    't4_verified': profile.t4_verified,
                    'notes': profile.notes,
                }
                for profile in PROFILES.values()
            ]
        })


class CsrfTokenView(PrivateResponseMixin, APIView):
    """Gives the static frontend a CSRF token and sets the `csrftoken` cookie.

    The page is a static file, so Django cannot embed the token in it. Send the
    token back in the X-CSRFToken header on POST requests.
    """

    authentication_classes = []

    def get(self, request):
        return Response({'csrf_token': get_token(request)})


@csrf_exempt
def api_not_found(request, *args, **kwargs):
    """JSON 404 for unknown /api/ paths, instead of Django's HTML page."""
    return api_error_response(request, 404, 'not_found', 'Not found.')


class HealthView(APIView):
    authentication_classes = []

    def get(self, request):
        return Response({'status': 'ok'})
