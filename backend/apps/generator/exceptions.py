"""Converts every API error into the documented shape:

{"error": {"code": "...", "message": "...", "request_id": "...", "fields": {...}}}
"""

import logging

from django.core.exceptions import RequestDataTooBig, SuspiciousOperation, TooManyFieldsSent, TooManyFilesSent
from django.http import JsonResponse
from django.utils.cache import patch_vary_headers
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler, set_rollback

logger = logging.getLogger(__name__)

DEFAULT_CODES = {
    status.HTTP_400_BAD_REQUEST: 'invalid_request',
    status.HTTP_401_UNAUTHORIZED: 'not_authenticated',
    status.HTTP_403_FORBIDDEN: 'forbidden',
    status.HTTP_404_NOT_FOUND: 'not_found',
    status.HTTP_405_METHOD_NOT_ALLOWED: 'method_not_allowed',
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: 'request_too_large',
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: 'unsupported_media_type',
    status.HTTP_429_TOO_MANY_REQUESTS: 'rate_limited',
}

REQUEST_TOO_LARGE_MESSAGE = 'The request is too large. Images must be 25 MB or smaller.'


def error_body(request, code, message):
    return {'error': {'code': code, 'message': message, 'request_id': getattr(request, 'request_id', None)}}


def api_error_response(request, http_status, code, message):
    """The standard error shape, for code that runs outside DRF (middleware, plain views).

    Marked private like the views' own errors (PrivateResponseMixin): these
    answer requests to private endpoints before any view runs.
    """
    response = JsonResponse(error_body(request, code, message), status=http_status)
    response['Cache-Control'] = 'private, no-store'
    patch_vary_headers(response, ('Cookie',))
    return response


def _first_error(detail):
    """Return (message, code) for the first error inside nested DRF details."""
    if isinstance(detail, dict):
        for value in detail.values():
            return _first_error(value)
    if isinstance(detail, list) and detail:
        return _first_error(detail[0])
    return str(detail), getattr(detail, 'code', None)


def _unhandled(exc, request):
    """Errors DRF does not handle itself: Django's request-parsing limits and bugs."""
    if isinstance(exc, RequestDataTooBig):
        # JSON / form bodies, or the non-file fields of a multipart request,
        # above DATA_UPLOAD_MAX_MEMORY_SIZE.
        return status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, 'request_too_large', 'The request is too large.'
    if isinstance(exc, (TooManyFieldsSent, TooManyFilesSent)):
        return status.HTTP_400_BAD_REQUEST, 'invalid_request', 'The request has too many form fields or files.'
    if isinstance(exc, SuspiciousOperation):
        return status.HTTP_400_BAD_REQUEST, 'invalid_request', 'The request could not be processed.'
    # Details stay in the server log; clients never see tracebacks or paths.
    logger.error('Unhandled API error (request %s)', getattr(request, 'request_id', None), exc_info=exc)
    return status.HTTP_500_INTERNAL_SERVER_ERROR, 'internal_error', 'An unexpected error occurred. Please try again.'


def api_exception_handler(exc, context):
    request = context.get('request')
    response = exception_handler(exc, context)
    if response is None:
        set_rollback()
        http_status, code, message = _unhandled(exc, request)
        return Response(error_body(request, code, message), status=http_status)

    message, code = _first_error(response.data)
    if isinstance(exc, ValidationError):
        if code in (None, 'invalid', 'required', 'blank', 'null', 'max_length', 'min_value', 'max_value', 'invalid_choice'):
            code = 'validation_error'
    else:
        code = DEFAULT_CODES.get(response.status_code, getattr(exc, 'default_code', 'error'))

    error = error_body(request, code, message)['error']
    if isinstance(exc, ValidationError) and isinstance(response.data, dict):
        error['fields'] = response.data
    response.data = {'error': error}
    return response
