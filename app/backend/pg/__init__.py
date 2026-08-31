"""PostgreSQL persistence layer for the production application.

Everything here sits behind interfaces so the hosting decision stays reversible.
The unresolved AppSail egress question must never reach business logic: services
depend on `Database` and `Session`, not on how the connection was obtained.
"""
from .config import (
    DatabaseConfig,
    EnvSecretProvider,
    MappingSecretProvider,
    SecretProvider,
    SecretUnavailable,
    from_env,
    get_secret_provider,
    set_secret_provider,
)
from .engine import Database, Scope, Session, get_database, set_database

__all__ = [
    "DatabaseConfig", "SecretProvider", "SecretUnavailable",
    "EnvSecretProvider", "MappingSecretProvider",
    "from_env", "get_secret_provider", "set_secret_provider",
    "Database", "Scope", "Session", "get_database", "set_database",
]
