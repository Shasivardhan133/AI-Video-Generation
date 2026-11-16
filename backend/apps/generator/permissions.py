from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import SAFE_METHODS, BasePermission


class CsrfProtectedWrites(BasePermission):
    """Require a valid CSRF token on every unsafe request, guests included.

    DRF's SessionAuthentication only checks CSRF for signed-in users, but most
    Geetha users are anonymous guests who own jobs through their session cookie.
    Failures raise PermissionDenied ("CSRF Failed: ..."), i.e. a 403 in the
    standard API error format.
    """

    def has_permission(self, request, view):
        if request.method not in SAFE_METHODS:
            SessionAuthentication().enforce_csrf(request)
        return True
