"""V3 Feishu tenant identity helpers.

Importing this package is opt-in only. It does not switch the legacy
Bitable-backed runtime path to V3 storage.
"""

from .resolver import IdentityResolutionError, resolve_or_create_user_context
from .user_context import UserContext

__all__ = [
    "IdentityResolutionError",
    "UserContext",
    "resolve_or_create_user_context",
]
