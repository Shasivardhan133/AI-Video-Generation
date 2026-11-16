import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent

load_dotenv(PROJECT_ROOT / '.env')

# The Django-side AI service client lives in the project-root `ai_service` package.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in {'1', 'true', 'yes'}


def env_positive_int(name, default):
    """A whole number of at least 1 from the environment; anything else stops startup."""
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    if not raw.isascii() or not raw.isdigit() or int(raw) < 1:
        raise ImproperlyConfigured(f'{name} must be a whole number of at least 1.')
    return int(raw)


AI_SERVICE_MODES = ('mock', 'remote')


def resolve_ai_service_mode(raw):
    """'mock' (the default, also when blank) or 'remote'; anything else stops startup,
    so a misspelt mode can never silently fall back to mock test videos."""
    mode = (raw or '').strip().lower() or 'mock'
    if mode not in AI_SERVICE_MODES:
        raise ImproperlyConfigured("AI_SERVICE_MODE must be 'mock' or 'remote'.")
    return mode


DEVELOPMENT_SECRET_KEY = 'development-only-key-replace-before-deployment'
# Publicly known keys that must never be used with DEBUG off: the development
# fallback above and the example value in the project-root .env.example.
PLACEHOLDER_SECRET_KEYS = frozenset({DEVELOPMENT_SECRET_KEY, 'replace-with-a-local-secret'})


def resolve_secret_key(configured, debug):
    """Return the secret key to use. With DEBUG off, a real key is required.

    The key is used exactly as configured (never trimmed or logged). Only in
    DEBUG mode does a missing or blank key fall back to the development key,
    and only in DEBUG mode is a published placeholder key accepted.
    """
    if configured and configured.strip():
        if not debug and configured.strip() in PLACEHOLDER_SECRET_KEYS:
            raise ImproperlyConfigured(
                'DJANGO_SECRET_KEY is set to a published placeholder value. '
                'Generate a unique secret key when DJANGO_DEBUG is off.'
            )
        return configured
    if debug:
        return DEVELOPMENT_SECRET_KEY
    raise ImproperlyConfigured(
        'DJANGO_SECRET_KEY is missing or blank. Set it in the environment or the '
        'project-root .env file when DJANGO_DEBUG is off.'
    )


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv('DJANGO_DEBUG', 'False').lower() in {'1', 'true', 'yes'}

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = resolve_secret_key(os.getenv('DJANGO_SECRET_KEY'), DEBUG)

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
    if host.strip()
]

# HTTPS reverse proxy. Behind a proxy that terminates TLS, Django only sees plain
# HTTP, so absolute URLs it builds (the history API's `next`/`previous` links)
# and its CSRF Origin check would use http://. Enable this only when every
# request reaches Django through a proxy that sets X-Forwarded-Proto itself,
# replacing any value sent by the client. Off by default: the header is then
# ignored, because any client can send it.
BEHIND_HTTPS_PROXY = env_bool('DJANGO_BEHIND_HTTPS_PROXY', False)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if BEHIND_HTTPS_PROXY else None
# X-Forwarded-Host is never trusted (USE_X_FORWARDED_HOST stays False): the proxy
# must pass the original Host header, which ALLOWED_HOSTS then validates.


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'apps.generator',
]

MIDDLEWARE = [
    'apps.generator.middleware.RequestIDMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # Before CommonMiddleware: JSON 404 instead of APPEND_SLASH errors for API writes.
    'apps.generator.middleware.ApiTrailingSlashMiddleware',
    # Before anything that reads the request body (sessions/CSRF/DRF parsing).
    'apps.generator.middleware.GenerationRequestSizeMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('MYSQL_DATABASE', 'ai_video_generator'),
        'USER': os.getenv('MYSQL_USER', ''),
        'PASSWORD': os.getenv('MYSQL_PASSWORD', ''),
        'HOST': os.getenv('MYSQL_HOST', '127.0.0.1'),
        'PORT': os.getenv('MYSQL_PORT', '3306'),
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'

MEDIA_ROOT = PROJECT_ROOT / os.getenv('MEDIA_ROOT', 'media')
MEDIA_URL = os.getenv('MEDIA_URL', '/media/')

# Static frontend (HTML/CSS/JS) served by Django during development.
FRONTEND_DIR = PROJECT_ROOT / 'frontend'

# Request and upload sizes. 1 MB here = 1,048,576 bytes (MiB).
# - Images may be up to 25 MB (apps.generator.validators.MAX_IMAGE_BYTES).
# - A whole generation request may be up to 26 MB, so a 25 MB image plus the
#   other form fields fits. It is checked against the declared Content-Length
#   before the body is read (GenerationRequestSizeMiddleware). Production should
#   set the same limit at the reverse proxy / hosting layer.
# - DATA_UPLOAD_MAX_MEMORY_SIZE is left at Django's default (2.5 MB). It limits
#   the non-file form fields of a request, not uploaded files.
GENERATION_MAX_REQUEST_BYTES = 26 * 1024 * 1024
# Uploaded files up to this size are kept in memory; larger ones are written to
# a temporary file while the request is handled.
FILE_UPLOAD_MAX_MEMORY_SIZE = 11 * 1024 * 1024

# One video at a time per owner (signed-in user or guest session): a generation
# request while the owner has a queued or processing job gets 409
# `generation_in_progress` (apps.generator.active_jobs). Deliberately not an
# environment variable. The check relies on job creation committing at once,
# so ATOMIC_REQUESTS must stay off.
GENERATION_MAX_ACTIVE_JOBS_PER_OWNER = 1


# Django REST Framework

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        # Anonymous demo users are allowed; ownership is enforced per session.
        # Writes still need a CSRF token, for guests as well as signed-in users.
        'apps.generator.permissions.CsrfProtectedWrites',
    ],
    'DEFAULT_PAGINATION_CLASS': 'apps.generator.pagination.HistoryPagination',
    'EXCEPTION_HANDLER': 'apps.generator.exceptions.api_exception_handler',
    # Used only by the generation endpoints (apps.generator.throttles); both
    # rates apply to one allowance shared by text- and image-to-video.
    'DEFAULT_THROTTLE_RATES': {
        'generation_burst': '5/minute',
        'generation_sustained': '30/hour',
    },
}


# AI service

# 'mock' renders a local FFmpeg test pattern; 'remote' calls the Colab service.
AI_SERVICE_MODE = resolve_ai_service_mode(os.getenv('AI_SERVICE_MODE'))
AI_SERVICE_BASE_URL = os.getenv('AI_SERVICE_BASE_URL', '')
AI_SERVICE_API_KEY = os.getenv('AI_SERVICE_API_KEY', '')
# Remote client limits (ai_service/client/remote.py), whole numbers of at least 1:
# - AI_SERVICE_TIMEOUT: seconds for one HTTP request (connect, or one read).
# - AI_SERVICE_POLL_INTERVAL: seconds between status checks.
# - AI_SERVICE_MAX_WAIT: overall deadline in seconds for one generation, covering
#   submission, polling and the video download. It can be overrun by at most one
#   read (AI_SERVICE_TIMEOUT), so a worker's job runs at most about
#   AI_SERVICE_MAX_WAIT + AI_SERVICE_TIMEOUT seconds; its heartbeat keeps the job's
#   lease alive meanwhile, and the job then fails with `service_timeout`.
# - AI_SERVICE_MAX_VIDEO_BYTES: largest video accepted from the service (default
#   100 MiB); bigger downloads stop and fail with `output_too_large`.
AI_SERVICE_TIMEOUT = env_positive_int('AI_SERVICE_TIMEOUT', 30)
AI_SERVICE_POLL_INTERVAL = env_positive_int('AI_SERVICE_POLL_INTERVAL', 10)
AI_SERVICE_MAX_WAIT = env_positive_int('AI_SERVICE_MAX_WAIT', 1800)
AI_SERVICE_MAX_VIDEO_BYTES = env_positive_int('AI_SERVICE_MAX_VIDEO_BYTES', 100 * 1024 * 1024)

# Start the job worker inside `runserver` so one command runs the whole app.
RUN_WORKER_IN_PROCESS = env_bool('RUN_WORKER_IN_PROCESS', True)

# Worker leases (apps.generator.worker). A processing job's heartbeat is
# refreshed every WORKER_HEARTBEAT_SECONDS; a job with no heartbeat for
# WORKER_STALE_AFTER_SECONDS is treated as abandoned and failed. Each running
# worker looks for abandoned jobs every WORKER_RECOVERY_INTERVAL_SECONDS.
WORKER_HEARTBEAT_SECONDS = 30
WORKER_STALE_AFTER_SECONDS = 300
WORKER_RECOVERY_INTERVAL_SECONDS = 60
# Queue timeout. A job still `queued` this long after it was created is failed
# with code `queue_timeout` and is never started (apps.generator.worker,
# expire_queued_jobs). Expiry runs inside every worker's recovery cycle, so it
# needs a running worker; without one, run `python manage.py expire_stale_jobs`
# (for example from a scheduler). Choose a value above the longest expected wait
# behind other jobs, or queued jobs of a busy single worker will expire.
GENERATION_QUEUE_TIMEOUT_SECONDS = env_positive_int('GENERATION_QUEUE_TIMEOUT_SECONDS', 30 * 60)


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'simple': {'format': '%(asctime)s %(levelname)s %(name)s: %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'}},
    'loggers': {'apps': {'handlers': ['console'], 'level': 'INFO'}},
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
