"""Rate limits for the two generation endpoints (they share one allowance).

Identity: signed-in users by user ID; guests by the direct client address
(REMOTE_ADDR). Forwarded headers such as X-Forwarded-For are ignored because a
client can set them freely; revisit this if Django is ever put behind a proxy.

Counters live in Django's default cache. The development cache is in-memory, so
counters reset when the server restarts and are not shared between processes.
"""

from rest_framework.exceptions import Throttled
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle


class GenerationRateThrottle(SimpleRateThrottle):
    cache_format = 'throttle_generation_%(scope)s_%(ident)s'

    def get_rate(self):
        # DRF's default reads the rates once at import time; read them per request
        # so settings overrides (for example in tests) take effect.
        return api_settings.DEFAULT_THROTTLE_RATES[self.scope]

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = f'user-{request.user.pk}'
        else:
            ident = f'ip-{request.META.get("REMOTE_ADDR", "")}'
        return self.cache_format % {'scope': self.scope, 'ident': ident}


class GenerationBurstThrottle(GenerationRateThrottle):
    scope = 'generation_burst'


class GenerationSustainedThrottle(GenerationRateThrottle):
    scope = 'generation_sustained'


class GenerationThrottled(Throttled):
    default_detail = 'Too many video requests.'
    extra_detail_singular = 'Please wait {wait} second before trying again.'
    extra_detail_plural = 'Please wait {wait} seconds before trying again.'
