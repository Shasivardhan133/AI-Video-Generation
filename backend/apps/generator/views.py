from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import GenerationJob
from .profiles import PROFILES
from .serializers import (
    ImageToVideoSerializer,
    JobListSerializer,
    JobSerializer,
    TextToVideoSerializer,
)


def _session_key(request):
    """Anonymous users own jobs through their session; create one if needed."""
    if not request.session.session_key:
        request.session.save()
    return request.session.session_key


def owned_jobs(request):
    if request.user.is_authenticated:
        return GenerationJob.objects.filter(user=request.user)
    return GenerationJob.objects.filter(user__isnull=True, session_key=_session_key(request))


class _CreateJobView(APIView):
    serializer_class = None

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        job = GenerationJob.objects.create(
            user=request.user if request.user.is_authenticated else None,
            session_key='' if request.user.is_authenticated else _session_key(request),
            mode=self.serializer_class.mode,
            prompt_text=data.get('prompt', ''),
            settings=data['settings'],
            input_file=data.get('image') or '',
            request_id=request.request_id,
        )
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
    parser_classes = [JSONParser, FormParser, MultiPartParser]


class ImageToVideoView(_CreateJobView):
    serializer_class = ImageToVideoSerializer
    parser_classes = [MultiPartParser, FormParser]


class JobListView(generics.ListAPIView):
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


class JobDetailView(generics.RetrieveAPIView):
    serializer_class = JobSerializer
    lookup_url_kwarg = 'job_id'

    def get_queryset(self):
        return owned_jobs(self.request)


class JobVideoView(APIView):
    def get(self, request, job_id):
        # Jobs that belong to someone else look identical to missing jobs.
        job = get_object_or_404(owned_jobs(request), pk=job_id)
        if not job.video_available or not job.output_file.storage.exists(job.output_file.name):
            raise NotFound('This video is not available yet.')
        download = request.query_params.get('download') == '1'
        return FileResponse(
            job.output_file.open('rb'),
            as_attachment=download,
            filename=f'generation-{job.id}.mp4',
            content_type='video/mp4',
        )


class ProfileListView(APIView):
    """Lets the frontend show only settings the backend will accept."""

    def get(self, request):
        return Response({
            'results': [
                {
                    'name': profile.name,
                    'mode': profile.mode,
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


class HealthView(APIView):
    authentication_classes = []

    def get(self, request):
        return Response({'status': 'ok'})
