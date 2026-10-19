from .mock import MockClient
from .remote import RemoteClient


def get_client(settings):
    """Build the client selected by AI_SERVICE_MODE ('mock' or 'remote')."""
    if settings.AI_SERVICE_MODE == 'remote':
        return RemoteClient(
            base_url=settings.AI_SERVICE_BASE_URL,
            api_key=settings.AI_SERVICE_API_KEY,
            request_timeout=settings.AI_SERVICE_TIMEOUT,
            poll_interval=settings.AI_SERVICE_POLL_INTERVAL,
            max_wait=settings.AI_SERVICE_MAX_WAIT,
        )
    return MockClient()
