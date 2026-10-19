"""
URL configuration for config project.

- /api/v1/ : the generation REST API (apps.generator).
- /admin/  : Django admin.
- /        : the static frontend from the project-root `frontend/` folder.
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('apps.generator.urls')),
    # Development-only frontend serving; use a real web server in production.
    path('', serve, {'path': 'pages/index.html', 'document_root': settings.FRONTEND_DIR}),
    re_path(r'^(?P<path>(?:css|js|assets)/.+)$', serve, {'document_root': settings.FRONTEND_DIR}),
]
