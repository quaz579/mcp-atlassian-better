"""Loads, merges, and validates configuration into an :class:`AppConfig`."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp_atlassian_better.errors import ConfigError
from mcp_atlassian_better.model import AppConfig, Defaults, SiteConfig, UpstreamConfig
from mcp_atlassian_better.secrets import Secret
from mcp_atlassian_better.sources import (
    ConfigSource,
    DropInSitesSource,
    EnvOverlaySource,
    RawConfig,
    TomlFileConfigSource,
    merge_sources,
)
from mcp_atlassian_better.tools_meta import CURATED_TOOLS, PROJECT_KEY_RE, WRAPPER_OWNED_TOOLS

_logger = logging.getLogger(__name__)

_SITE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_KEY_PREFIX_RE = PROJECT_KEY_RE
_TOOLSET_PRESETS = ("curated", "all")

# WRAPPER_OWNED_TOOLS names are still valid for `enabled_tools`: they're part
# of the curated tool set, just served by the wrapper itself (see
# tools_meta.WRAPPER_OWNED_TOOLS) rather than forwarded to the child. Written
# as a union (not just CURATED_TOOLS) so this stays correct if a future
# wrapper-owned tool is ever added outside the curated set.
_CURATED_ALLOWLIST = CURATED_TOOLS | WRAPPER_OWNED_TOOLS


_SITES_DIR_ENV = "MCP_ATLASSIAN_BETTER_SITES_DIR"


def resolve_config_path() -> Path:
    """Where ``load_config`` reads from when no explicit sources are given."""
    env_path = os.environ.get("MCP_ATLASSIAN_BETTER_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home).expanduser() if xdg_config_home else Path.home() / ".config"
    return base / "mcp-atlassian-better" / "config.toml"


def resolve_sites_dir(config_path: Path) -> Path:
    """Where the ``sites.d`` drop-in directory lives for a given config file.

    Overridable via ``MCP_ATLASSIAN_BETTER_SITES_DIR`` so an integration (e.g. a
    dashboard) can point it somewhere other than next to ``config.toml``.
    """
    env_dir = os.environ.get(_SITES_DIR_ENV)
    if env_dir:
        return Path(env_dir).expanduser()
    return config_path.parent / "sites.d"


def default_sources(config_path: Path) -> list[ConfigSource]:
    """The real, non-test source list for a given config file path.

    Centralized so both entry points that build sources from just a path --
    ``load_config(sources=None)`` and the CLI's ``--config PATH`` -- agree on
    what's loaded. Drop-in files are listed FIRST so ``config.toml`` overrides
    them per field, per site name.
    """
    return [
        DropInSitesSource(resolve_sites_dir(config_path)),
        TomlFileConfigSource(config_path),
        EnvOverlaySource(),
    ]


def load_config(sources: Sequence[ConfigSource] | None = None) -> AppConfig:
    if sources is None:
        sources = default_sources(resolve_config_path())
    loaded = [(source, source.load()) for source in sources]

    # Merge the non-overlay sources (e.g. the drop-in dir + the TOML file)
    # first, so we know which site names existed before any env overlay
    # touches them.
    base = merge_sources(*(raw for source, raw in loaded if not isinstance(source, EnvOverlaySource)))
    known_site_names = set(base["sites"])

    merged = merge_sources(base, *(raw for source, raw in loaded if isinstance(source, EnvOverlaySource)))
    _drop_incomplete_env_only_sites(merged, known_site_names)
    return _build_app_config(merged)


def _drop_incomplete_env_only_sites(raw: RawConfig, known_site_names: set[str]) -> None:
    """Drops a site that ONLY an env overlay introduced but didn't fully define.

    A stray ``MCP_ATLASSIAN_BETTER_SITE_<NAME>_*`` variable (typo, leftover from another
    tool) would otherwise create a new, incomplete site and hard-fail config
    loading. Overlaying a field onto a site the TOML file already defines is
    unaffected — this only guards a site whose *entire* definition comes from
    the environment.
    """
    for site_name in list(raw["sites"]):
        if site_name in known_site_names:
            continue
        fields = raw["sites"][site_name]
        if "url" in fields and "key_prefixes" in fields:
            continue
        _logger.warning(
            "ignoring incomplete site '%s' introduced only by a MCP_ATLASSIAN_BETTER_SITE_%s_* "
            "environment variable (needs at least _URL and _KEY_PREFIXES); "
            "set both or remove the variable",
            site_name,
            site_name.upper(),
        )
        del raw["sites"][site_name]
        raw.get("sources", {}).pop(site_name, None)


def _build_app_config(raw: RawConfig) -> AppConfig:
    sites_raw = raw["sites"]
    if not sites_raw:
        raise ConfigError("no sites configured; add at least one [[sites]] entry")

    defaults = _parse_defaults(raw["defaults"])
    upstream = _parse_upstream(raw["upstream"])
    provenance = raw.get("sources", {})

    sites: list[SiteConfig] = []
    prefix_owners: dict[str, str] = {}
    for name, fields in sites_raw.items():
        site = _parse_site(name, fields, defaults, source=provenance.get(name, "env"))
        for prefix in site.key_prefixes:
            owner = prefix_owners.get(prefix)
            if owner is not None and owner != site.name:
                raise ConfigError(
                    f"key prefix '{prefix}' is used by both site '{owner}' and site '{site.name}'"
                )
            prefix_owners[prefix] = site.name
        sites.append(site)

    return AppConfig(defaults=defaults, upstream=upstream, sites=tuple(sites))


def _parse_defaults(fields: dict[str, Any]) -> Defaults:
    toolset_preset = fields.get("toolset_preset", "curated")
    if toolset_preset not in _TOOLSET_PRESETS:
        raise ConfigError(
            f"defaults.toolset_preset must be one of {_TOOLSET_PRESETS}, got {toolset_preset!r}"
        )

    api_token, api_token_env = _split_token_fields(fields, "api_token", site_label="defaults")
    personal_token, personal_token_env = _split_token_fields(fields, "personal_token", site_label="defaults")
    if (api_token is not None or api_token_env is not None) and (
        personal_token is not None or personal_token_env is not None
    ):
        raise ConfigError(
            "[defaults]: specify either api_token/api_token_env or "
            "personal_token/personal_token_env, not both"
        )

    return Defaults(
        username=fields.get("username"),
        api_token=api_token,
        api_token_env=api_token_env,
        personal_token=personal_token,
        personal_token_env=personal_token_env,
        toolset_preset=toolset_preset,
        call_timeout_seconds=_positive_number(fields, "call_timeout_seconds", 120.0, "defaults"),
        connect_timeout_seconds=_positive_number(fields, "connect_timeout_seconds", 90.0, "defaults"),
        attachment_max_bytes=int(_positive_number(fields, "attachment_max_bytes", 104_857_600, "defaults")),
        recovery_cooldown_seconds=_positive_number(fields, "recovery_cooldown_seconds", 30.0, "defaults"),
        health_recovery_budget_seconds=_positive_number(
            fields, "health_recovery_budget_seconds", 8.0, "defaults"
        ),
    )


def _parse_upstream(fields: dict[str, Any]) -> UpstreamConfig:
    command = fields.get("command")
    env_passthrough = fields.get("env_passthrough")
    workspace_dir = fields.get("workspace_dir")
    defaults = UpstreamConfig()
    return UpstreamConfig(
        command=_parse_str_list(
            command, "upstream.command", default=defaults.command, require_non_empty=True
        ),
        env_passthrough=_parse_str_list(
            env_passthrough,
            "upstream.env_passthrough",
            default=defaults.env_passthrough,
            require_non_empty=False,
        ),
        workspace_dir=workspace_dir if workspace_dir is not None else defaults.workspace_dir,
    )


def _parse_str_list(
    value: Any, label: str, *, default: tuple[str, ...], require_non_empty: bool
) -> tuple[str, ...]:
    """Rejects a bare string (e.g. ``command = "uvx"``), which ``tuple()`` would
    silently explode into one-character elements instead of a single-item list."""
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{label} must be a list of non-empty strings, got {value!r}")
    if require_non_empty and not value:
        raise ConfigError(f"{label} must be a non-empty list of strings")
    return tuple(value)


def _positive_number(fields: dict[str, Any], key: str, default: float, label: str) -> float:
    value = fields.get(key, default)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label}.{key} must be a number, got {value!r}") from exc
    if number <= 0:
        raise ConfigError(f"{label}.{key} must be greater than zero, got {number}")
    return number


def _split_token_fields(
    fields: dict[str, Any], base: str, *, site_label: str
) -> tuple[Secret | None, str | None]:
    """Reads ``<base>``/``<base>_env`` from a raw dict without resolving env yet."""
    literal = fields.get(base)
    env_name = fields.get(f"{base}_env")
    if literal is not None and env_name is not None:
        raise ConfigError(f"{site_label}: specify only one of '{base}' or '{base}_env', not both")
    if literal is not None:
        return Secret(str(literal)), None
    return None, env_name


def _parse_site(name: str, fields: dict[str, Any], defaults: Defaults, *, source: str = "env") -> SiteConfig:
    if not _SITE_NAME_RE.match(name):
        raise ConfigError(f"site name '{name}' is invalid; names must match {_SITE_NAME_RE.pattern}")

    url = _parse_url(name, fields.get("url"))
    key_prefixes = _parse_key_prefixes(name, fields.get("key_prefixes"))

    username = fields.get("username", defaults.username)
    read_only = bool(fields.get("read_only", False))
    enabled_tools = _parse_enabled_tools(name, fields.get("enabled_tools"), defaults.toolset_preset)
    projects_filter = _parse_projects_filter(name, fields.get("projects_filter"))

    api_token, api_token_env = _split_token_fields(fields, "api_token", site_label=f"site '{name}'")
    personal_token, personal_token_env = _split_token_fields(
        fields, "personal_token", site_label=f"site '{name}'"
    )
    site_has_cloud = api_token is not None or api_token_env is not None
    site_has_personal = personal_token is not None or personal_token_env is not None
    if site_has_cloud and site_has_personal:
        raise ConfigError(
            f"site '{name}': specify either api_token/api_token_env "
            "or personal_token/personal_token_env, not both"
        )

    if not site_has_cloud and not site_has_personal:
        # Nothing of its own: inherit whichever auth group the defaults define.
        api_token, api_token_env = defaults.api_token, defaults.api_token_env
        personal_token, personal_token_env = defaults.personal_token, defaults.personal_token_env

    resolved_api_token, resolved_api_token_env = _resolve_token(
        api_token, api_token_env, site_name=name, field="api_token", own=site_has_cloud
    )
    resolved_personal_token, resolved_personal_token_env = _resolve_token(
        personal_token, personal_token_env, site_name=name, field="personal_token", own=site_has_personal
    )

    if resolved_api_token is not None and resolved_personal_token is not None:
        raise ConfigError(
            f"site '{name}': resolved both a Cloud api_token and a Server/DC personal_token; "
            "only one auth shape is allowed"
        )
    if resolved_api_token is None and resolved_personal_token is None:
        raise ConfigError(
            f"site '{name}': no credentials resolved; set api_token/api_token_env (Cloud) "
            "or personal_token/personal_token_env (Server/DC), directly or via [defaults]"
        )
    if resolved_api_token is not None and not username:
        raise ConfigError(
            f"site '{name}': Cloud auth (api_token) requires a username, set directly or via [defaults]"
        )

    return SiteConfig(
        name=name,
        url=url,
        key_prefixes=key_prefixes,
        username=username,
        api_token=resolved_api_token,
        api_token_env=resolved_api_token_env,
        personal_token=resolved_personal_token,
        personal_token_env=resolved_personal_token_env,
        read_only=read_only,
        enabled_tools=enabled_tools,
        projects_filter=projects_filter,
        source=source,
    )


def _parse_url(site_name: str, raw_url: Any) -> str:
    if not raw_url or not isinstance(raw_url, str):
        raise ConfigError(f"site '{site_name}': 'url' is required")
    parts = urlsplit(raw_url)
    if parts.username or parts.password:
        # Never echo raw_url here: it's exactly the thing that carries the leaked credential.
        raise ConfigError(
            f"site '{site_name}': 'url' must not embed credentials (user:pass@host); "
            "use api_token/api_token_env or personal_token/personal_token_env instead"
        )
    if parts.scheme != "https" or not parts.netloc:
        raise ConfigError(f"site '{site_name}': 'url' must be an https URL")
    return raw_url.rstrip("/")


def _parse_key_prefixes(site_name: str, raw_prefixes: Any) -> tuple[str, ...]:
    if not raw_prefixes or not isinstance(raw_prefixes, list):
        raise ConfigError(f"site '{site_name}': 'key_prefixes' must be a non-empty list")
    prefixes: list[str] = []
    for prefix in raw_prefixes:
        if not isinstance(prefix, str) or not _KEY_PREFIX_RE.match(prefix):
            raise ConfigError(
                f"site '{site_name}': key prefix {prefix!r} is invalid; must match {_KEY_PREFIX_RE.pattern}"
            )
        prefixes.append(prefix)
    return tuple(prefixes)


def _parse_enabled_tools(site_name: str, raw_tools: Any, toolset_preset: str) -> frozenset[str] | None:
    if raw_tools is None:
        return None
    if not isinstance(raw_tools, list) or not all(isinstance(t, str) for t in raw_tools):
        raise ConfigError(f"site '{site_name}': 'enabled_tools' must be a list of strings")
    tools = frozenset(raw_tools)
    if toolset_preset == "curated":
        unknown = tools - _CURATED_ALLOWLIST
        if unknown:
            raise ConfigError(
                f"site '{site_name}': enabled_tools contains tools outside the curated allowlist: "
                f"{', '.join(sorted(unknown))}"
            )
    else:
        not_jira = {t for t in tools if not t.startswith("jira_")}
        if not_jira:
            raise ConfigError(
                f"site '{site_name}': enabled_tools must all be 'jira_'-prefixed tool names: "
                f"{', '.join(sorted(not_jira))}"
            )
    return tools


def _parse_projects_filter(site_name: str, raw_value: Any) -> tuple[str, ...] | None:
    """Upstream's static ``JIRA_PROJECTS_FILTER`` (restricts which projects the
    child's search/browse tools see at all) — distinct from a tool call's own
    ``projects_filter`` argument, which only affects site *routing*.

    Normalized to uppercase and validated as project keys here so
    ``site_policy.enforce_site_policy``'s comparison (against an issue key's
    project, itself uppercased) never has to guess at casing, and so a typo
    that isn't even a valid project key shape is caught at load time instead
    of just silently never matching anything.
    """
    if raw_value is None:
        return None
    if not isinstance(raw_value, list) or not all(isinstance(item, str) and item for item in raw_value):
        raise ConfigError(f"site '{site_name}': 'projects_filter' must be a list of non-empty strings")
    if not raw_value:
        raise ConfigError(f"site '{site_name}': 'projects_filter' must not be empty when set")
    normalized: list[str] = []
    for item in raw_value:
        upper = item.strip().upper()
        if not _KEY_PREFIX_RE.match(upper):
            raise ConfigError(
                f"site '{site_name}': projects_filter entry {item!r} is invalid; must be a project "
                f"key matching {_KEY_PREFIX_RE.pattern} (a numeric project id is never a valid entry)"
            )
        normalized.append(upper)
    return tuple(normalized)


def _resolve_token(
    literal: Secret | None, env_name: str | None, *, site_name: str, field: str, own: bool
) -> tuple[Secret | None, str | None]:
    if literal is not None:
        origin = f"site '{site_name}'" if own else f"[defaults] (inherited by site '{site_name}')"
        _logger.info(
            "%s: %s is a literal value (from the config file or a MCP_ATLASSIAN_BETTER_SITE_* variable, "
            "not %s_env); consider using %s_env instead",
            origin,
            field,
            field,
            field,
        )
        return literal, None
    if env_name is not None:
        value = os.environ.get(env_name)
        if value is None:
            raise ConfigError(
                f"site '{site_name}': environment variable '{env_name}' referenced by {field}_env is not set"
            )
        if value == "":
            raise ConfigError(
                f"site '{site_name}': environment variable '{env_name}' referenced by {field}_env "
                "is set but empty"
            )
        return Secret(value), env_name
    return None, None
