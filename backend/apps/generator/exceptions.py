"""Converts every API error into the documented shape:

{"error": {"code": "...", "message": "...", "request_id": "...", "fields": {...}}}
"""

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.views import exception_handler

DEFAULT_CODES = {
    status.HTTP_400_BAD_REQUEST: 'invalid_request',
    status.HTTP_401_UNAUTHORIZED: 'not_authenticated',
    status.HTTP_403_FORBIDDEN: 'forbidden',
    status.HTTP_404_NOT_FOUND: 'not_found',
    status.HTTP_405_METHOD_NOT_ALLOWED: 'method_not_allowed',
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: 'unsupported_media_type',
    status.HTTP_429_TOO_MANY_REQUESTS: 'rate_limited',
}


def _first_error(detail):
    """Return (message, code) for the first error inside nested DRF details."""
    if isinstance(detail, dict):
        for value in detail.values():
            return _first_error(value)
    if isinstance(detail, list) and detail:
        return _first_error(detail[0])
    return str(detail), getattr(detail, 'code', None)


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    request = context.get('request')
    message, code = _first_error(response.data)
    if isinstance(exc, ValidationError):
        if code in (None, 'invalid', 'required', 'blank', 'null', 'max_length', 'min_value', 'max_value', 'invalid_choice'):
            code = 'validation_error'
    else:
        code = DEFAULT_CODES.get(response.status_code, getattr(exc, 'default_code', 'error'))

    error = {
        'code': code,
        'message': message,
        'request_id': getattr(request, 'request_id', None),
    }
    if isinstance(exc, ValidationError) and isinstance(response.data, dict):
        error['fields'] = response.data
    response.data = {'error': error}
    return response
