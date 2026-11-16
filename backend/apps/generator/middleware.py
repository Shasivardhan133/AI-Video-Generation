import re
import uuid

from django.conf import settings
from django.urls import is_valid_path

from .exceptions import REQUEST_TOO_LARGE_MESSAGE, api_error_response

SAFE_REQUEST_ID = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
CONTENT_LENGTH = re.compile(r'^[0-9]{1,20}$')


class RequestIDMiddleware:
    """Gives every request a correlation ID, echoed in the X-Request-ID header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get('X-Request-ID', '')
        request.request_id = incoming if SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        response = self.get_response(request)
        response['X-Request-ID'] = request.request_id
        return response


class ApiTrailingSlashMiddleware:
    """JSON 404 for API requests without the trailing slash, except GET and HEAD.

    Every API URL ends with '/'. For GET and HEAD, CommonMiddleware's
    APPEND_SLASH redirect still applies. For other methods Django cannot redirect
    without losing the request body: with DEBUG on it raises RuntimeError (an
    HTML 500 page) and with DEBUG off it sends an HTML 301 that clients follow
    as a GET. Such requests are answered here instead, before CommonMiddleware,
    sessions, CSRF and the views run, so nothing is created or rate limited.
    """

    REDIRECTED_METHODS = {'GET', 'HEAD'}
    MESSAGE = 'Not found. API URLs must end with a slash.'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if (
            request.method not in self.REDIRECTED_METHODS
            and (path == '/api' or path.startswith('/api/'))
            and not path.endswith('/')
            and not is_valid_path(path, getattr(request, 'urlconf', None))
        ):
            return api_error_response(request, 404, 'not_found', self.MESSAGE)
        return self.get_response(request)


class GenerationRequestSizeMiddleware:
    """Rejects oversized generation requests before anything reads the body.

    Runs before the CSRF check and the rate limit (both of which parse the body),
    so an oversized request creates nothing, stores nothing and uses no rate-limit
    allowance. Only the declared Content-Length is checked. Under WSGI Django
    never reads more than that length (a request without one is read as empty),
    so this bounds what the application parses; it does not stop a client from
    sending more bytes to the web server, which needs its own upload limit.
    """

    PATH_PREFIX = '/api/v1/generations/'
    BODY_METHODS = {'POST', 'PUT', 'PATCH'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in self.BODY_METHODS and request.path.startswith(self.PATH_PREFIX):
            declared = request.META.get('CONTENT_LENGTH', '')
            if declared:
                if not CONTENT_LENGTH.match(declared):
                    return api_error_response(request, 400, 'invalid_request', 'Invalid Content-Length header.')
                if int(declared) > settings.GENERATION_MAX_REQUEST_BYTES:
                    return api_error_response(request, 413, 'request_too_large', REQUEST_TOO_LARGE_MESSAGE)
        return self.get_response(request)
