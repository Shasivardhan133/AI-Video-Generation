"""
URL configuration for config project.

- /api/v1/ : the generation REST API (apps.generator). Unknown /api/ paths
             return a JSON 404 in the API's error format.
- /admin/  : Django admin.
- /        : the static frontend from the project-root `frontend/` folder.
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

from apps.generator.views import api_not_found

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('apps.generator.urls')),
    # Only paths ending in '/' match, so APPEND_SLASH redirects keep working.
    re_path(r'^api/(.*/)?$', api_not_found),
    # Development-only frontend serving; use a real web server in production.
    path('', serve, {'path': 'pages/index.html', 'document_root': settings.FRONTEND_DIR}),
    re_path(r'^(?P<path>(?:css|js|assets)/.+)$', serve, {'document_root': settings.FRONTEND_DIR}),
]
