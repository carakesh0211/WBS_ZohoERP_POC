"""Database configuration and the secret-provider boundary.

Two rules shape this module.

**A password is never a configuration value.** It is fetched, used, and not
retained anywhere that can be printed, logged, serialised or committed. The DSN
is assembled at connection time and discarded; `__repr__` and `__str__` are
overridden so a config object cannot leak one through an exception, a log line
or a debugger.

**The provider is an interface.** Development reads the environment. A hosted
deployment will read Catalyst Secret Management, or a cloud secret manager, or a
file mounted by an orchestrator. Business logic must never know which, so
nothing outside this module constructs a DSN.
"""
from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Protocol


class SecretUnavailable(RuntimeError):
    """A required secret is not obtainable. Carries a NAME, never a value."""


class SecretProvider(Protocol):
    """Where secrets come from. The only thing the app is allowed to assume."""

    def get(self, name: str) -> str | None: ...


class EnvSecretProvider:
    """Development and CI. Reads the process environment."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name)


class MappingSecretProvider:
    """Tests, and any caller holding secrets in memory already."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str | None:
        return self._values.get(name)


#: Overridable so a deployment can install its own provider once, at start-up,
#: without every call site learning about it.
_provider: SecretProvider = EnvSecretProvider()


def set_secret_provider(provider: SecretProvider) -> None:
    global _provider
    _provider = provider


def get_secret_provider() -> SecretProvider:
    return _provider


@dataclass(frozen=True)
class DatabaseConfig:
    """Everything needed to reach PostgreSQL except the password.

    The password is deliberately absent from the dataclass. It is resolved from
    the provider at connection time by :meth:`dsn`, so it never lands in a field
    that could be pickled, repr'd or logged.
    """

    host: str
    port: int = 5432
    database: str = "capex"
    user: str = "capex_app"
    sslmode: str = "verify-full"
    sslrootcert: str | None = None
    connect_timeout: int = 10
    application_name: str = "capex-wbs-hub"
    #: Name looked up in the secret provider, not the secret itself.
    password_secret_name: str = "CAPEX_DB_PASSWORD"
    options: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ safety
    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return (f"DatabaseConfig(host={self.host!r}, port={self.port}, "
                f"database={self.database!r}, user={self.user!r}, "
                f"sslmode={self.sslmode!r}, password=<not stored>)")

    __str__ = __repr__

    # ------------------------------------------------------------------ dsn
    def dsn(self, provider: SecretProvider | None = None) -> str:
        """Assemble a libpq DSN. **The return value is a secret.**

        Never log it, never put it in an exception, never write it to evidence.
        Callers should pass it straight to the driver and let it go out of scope.
        """
        source = provider if provider is not None else get_secret_provider()
        password = source.get(self.password_secret_name)
        if not password:
            raise SecretUnavailable(
                f"no value for secret {self.password_secret_name!r}; "
                f"configure the secret provider before connecting")

        parts = {
            "host": self.host,
            "port": str(self.port),
            "dbname": self.database,
            "user": self.user,
            "password": password,
            "sslmode": self.sslmode,
            "connect_timeout": str(self.connect_timeout),
            "application_name": self.application_name,
        }
        if self.sslrootcert:
            parts["sslrootcert"] = self.sslrootcert
        parts.update(self.options)
        return " ".join(f"{k}={_quote(v)}" for k, v in parts.items())

    def safe_summary(self) -> dict[str, object]:
        """Loggable. Contains nothing secret, by construction."""
        return {"host": self.host, "port": self.port, "database": self.database,
                "user": self.user, "sslmode": self.sslmode,
                "sslrootcert_configured": self.sslrootcert is not None}


def _quote(value: str) -> str:
    if value == "" or any(c in value for c in " '\\"):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return value


def from_env(prefix: str = "CAPEX_DB_") -> DatabaseConfig:
    """Build a config from the environment. Reads no password.

    `CAPEX_DB_URL` is accepted for local convenience and CI service containers,
    where a single URL is the ergonomic form. Any password embedded in it is
    moved into an in-memory provider immediately rather than being retained on
    the config object.
    """
    url = os.environ.get(f"{prefix}URL")
    if url:
        return _from_url(url)

    host = os.environ.get(f"{prefix}HOST")
    if not host:
        raise SecretUnavailable(
            f"neither {prefix}URL nor {prefix}HOST is set; "
            f"the database is not configured")
    return DatabaseConfig(
        host=host,
        port=int(os.environ.get(f"{prefix}PORT", "5432")),
        database=os.environ.get(f"{prefix}NAME", "capex"),
        user=os.environ.get(f"{prefix}USER", "capex_app"),
        sslmode=os.environ.get(f"{prefix}SSLMODE", "verify-full"),
        sslrootcert=os.environ.get(f"{prefix}SSLROOTCERT") or None,
        password_secret_name=f"{prefix}PASSWORD",
    )


def _from_url(url: str) -> DatabaseConfig:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"unsupported database URL scheme: {parsed.scheme!r}")

    secret_name = "CAPEX_DB_URL_PASSWORD"
    if parsed.password:
        # Lift it out of the URL and into a provider, so it is not carried on
        # the config object where a repr could reach it.
        merged = dict(getattr(get_secret_provider(), "_values", {}) or {})
        merged[secret_name] = urllib.parse.unquote(parsed.password)
        set_secret_provider(_ChainedProvider(MappingSecretProvider(merged),
                                             get_secret_provider()))

    return DatabaseConfig(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        database=(parsed.path or "/capex").lstrip("/") or "capex",
        user=urllib.parse.unquote(parsed.username or "capex_app"),
        # verify-full, matching the CAPEX_DB_HOST path and the dataclass
        # default. This branch previously defaulted to `prefer`, which performs
        # NO certificate or hostname validation and silently falls back to
        # plaintext if the server declines TLS -- and it is the branch
        # `from_env()` takes first, so any URL-configured deployment was
        # downgraded without a word in the logs. Relaxing TLS must be a
        # deliberate act, so `prefer` now requires setting CAPEX_DB_SSLMODE.
        sslmode=os.environ.get("CAPEX_DB_SSLMODE", "verify-full"),
        password_secret_name=secret_name,
    )


class _ChainedProvider:
    """First provider wins; the second is the fallback."""

    def __init__(self, first: SecretProvider, second: SecretProvider) -> None:
        self._first, self._second = first, second

    def get(self, name: str) -> str | None:
        value = self._first.get(name)
        return value if value is not None else self._second.get(name)
