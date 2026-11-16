"""Backend test suite. Run from `backend/` with: python manage.py test tests

Safety: config/settings.py loads the developer's real .env, which may hold the
Colab service URL and API key. Before any test module is imported, the AI
service settings are forced, for the whole run, to the local mock with an
unroutable URL and no key, so no test can reach a real AI service even if .env
sets AI_SERVICE_MODE=remote (tests/test_safety.py checks this). Tests of the
remote client pass their own settings and talk to a fake service on 127.0.0.1.
"""

from django.test.utils import override_settings

NO_REAL_AI_SERVICE = override_settings(
    AI_SERVICE_MODE='mock', AI_SERVICE_BASE_URL='https://ai-service.invalid', AI_SERVICE_API_KEY='',
)
NO_REAL_AI_SERVICE.enable()
