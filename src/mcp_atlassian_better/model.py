"""Frozen dataclasses for the loaded, validated configuration."""

from __future__ import annotations

from dataclasses import dataclass

from mcp_atlassian_better.secrets import Secret


@dataclass(frozen=True, slots=True)
class Defaults:
    """Fallback values applied to a site when it doesn't set its own."""

    username: str | None = None
    api_token: Secret | None = None
    api_token_env: str | None = None
    personal_token: Secret | None = None
    personal_token_env: str | None = None
    toolset_preset: str = "curated"
    call_timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 90.0
    attachment_max_bytes: int = 104_857_600
    recovery_cooldown_seconds: float = 30.0
    health_recovery_budget_seconds: float = 8.0


@dataclass(frozen=True, slots=True)
class UpstreamConfig:
    """How to launch the upstream ``mcp-atlassian`` child process."""

    command: tuple[str, ...] = ("uvx", "mcp-atlassian@latest")
    env_passthrough: tuple[str, ...] = (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    )
    workspace_dir: str = "."


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """One fully resolved Jira site: exactly one auth shape, ready to use.

    ``api_token``/``personal_token`` hold the resolved secret value (from the
    file or from the environment variable named by the matching ``*_env``
    field); the ``*_env`` field is kept only so ``--print-config`` can show
    where the value came from without showing the value itself. ``source`` is
    the config file (or drop-in file, or ``"env"``) that defined this site --
    shown by ``--print-config``/``--check``/``jira_sites`` for provenance,
    never used for anything semantic.
    """

    name: str
    url: str
    key_prefixes: tuple[str, ...]
    username: str | None = None
    api_token: Secret | None = None
    api_token_env: str | None = None
    personal_token: Secret | None = None
    personal_token_env: str | None = None
    read_only: bool = False
    enabled_tools: frozenset[str] | None = None
    projects_filter: tuple[str, ...] | None = None
    source: str = "unknown"

    @property
    def auth_mode(self) -> str:
        if self.personal_token is not None:
            return "server_dc"
        if self.api_token is not None:
            return "cloud"
        raise ValueError(f"site '{self.name}' has no resolved credentials")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """The fully loaded and validated application configuration."""

    defaults: Defaults
    upstream: UpstreamConfig
    sites: tuple[SiteConfig, ...]
