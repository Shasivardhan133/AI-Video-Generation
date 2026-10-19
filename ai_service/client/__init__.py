"""Django-side client for the separate GPU inference service."""

from .base import AIServiceError, GenerationRequest
from .factory import get_client

__all__ = ['AIServiceError', 'GenerationRequest', 'get_client']
