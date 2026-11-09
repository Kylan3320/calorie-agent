"""V3 storage interfaces.

These modules are opt-in only. Importing them does not switch the legacy
Bitable-backed online path to MySQL.
"""

from .mysql_client import (
    MySQLConfig,
    StorageConfigError,
    execute,
    fetch_all,
    fetch_one,
    get_connection,
)
from .repositories import (
    DEFAULT_DAILY_STANDARD,
    FoodRepository,
    InMemoryRepository,
    IntakeRepository,
    MemoryRepository,
    MetricsRepository,
    MySQLStorageRepository,
    StandardRepository,
    TenantRepository,
    UserRepository,
)

__all__ = [
    "DEFAULT_DAILY_STANDARD",
    "FoodRepository",
    "InMemoryRepository",
    "IntakeRepository",
    "MemoryRepository",
    "MetricsRepository",
    "MySQLConfig",
    "MySQLStorageRepository",
    "StandardRepository",
    "StorageConfigError",
    "TenantRepository",
    "UserRepository",
    "execute",
    "fetch_all",
    "fetch_one",
    "get_connection",
]
