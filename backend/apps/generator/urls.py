from django.urls import path

from . import views

urlpatterns = [
    path('health/', views.HealthView.as_view(), name='health'),
    path('profiles/', views.ProfileListView.as_view(), name='profile-list'),
    path('generations/', views.JobListView.as_view(), name='job-list'),
    path('generations/text-to-video/', views.TextToVideoView.as_view(), name='text-to-video'),
    path('generations/image-to-video/', views.ImageToVideoView.as_view(), name='image-to-video'),
    path('generations/<uuid:job_id>/', views.JobDetailView.as_view(), name='job-detail'),
    path('generations/<uuid:job_id>/video/', views.JobVideoView.as_view(), name='job-video'),
]
