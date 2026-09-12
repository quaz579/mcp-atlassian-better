"""Config sources: where raw (unvalidated) configuration data comes from.

A source only parses and structures data; all semantic validation (auth
shapes, prefix uniqueness, env var resolution) happens in ``config.py`` so
every source produces errors in one consistent style.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, TypedDict

from mcp_atlassian_better.errors import ConfigError

_logger = logging.getLogger(__name__)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class _RawConfigRequired(TypedDict):
    defaults: dict[str, Any]
    upstream: dict[str, Any]
    sites: dict[str, dict[str, Any]]


class RawConfig(_RawConfigRequired, total=False):
    """``sources`` (site name -> the file/label that defined it) is optional.

    Split into a mixed-totality base + subclass rather than ``NotRequired``
    (Python 3.11+; this package's CI matrix includes 3.10) so every
    pre-existing ``RawConfig`` literal in the tests stays mypy-clean without
    restating it. Read it via ``.get("sources", {})``; a source that doesn't
    track provenance (e.g. ``EnvOverlaySource``) may simply omit it.
    """

    sources: dict[str, str]


def _empty_raw_config() -> RawConfig:
    return {"defaults": {}, "upstream": {}, "sites": {}, "sources": {}}


class ConfigSource(Protocol):
    name: str

    def load(self) -> RawConfig: ...


class TomlFileConfigSource:
    """Reads ``[defaults]``, ``[upstream]``, and ``[[sites]]`` from a TOML file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = f"file:{path}"

    def load(self) -> RawConfig:
        if not self.path.is_file():
            raise ConfigError(f"config file not found: {self.path}")
        self._warn_if_insecure_permissions()
        try:
            with self.path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{self.name}: invalid TOML: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{self.name}: file is not valid UTF-8: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{self.name}: could not read config file ({exc.__class__.__name__})") from exc

        sites: dict[str, dict[str, Any]] = {}
        for entry in data.get("sites", []):
            name = entry.get("name")
            if not name:
                raise ConfigError(f"{self.name}: a [[sites]] entry is missing the required 'name' field")
            if name in sites:
                raise ConfigError(f"{self.name}: duplicate [[sites]] entry for name '{name}'")
            sites[name] = dict(entry)

        return {
            "defaults": dict(data.get("defaults", {})),
            "upstream": dict(data.get("upstream", {})),
            "sites": sites,
            "sources": {name: str(self.path) for name in sites},
        }

    def _warn_if_insecure_permissions(self) -> None:
        try:
            mode = self.path.stat().st_mode
        except OSError:
            return
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            _logger.warning(
                "config file %s is readable by group/other (mode %o); consider `chmod 600`",
                self.path,
                stat.S_IMODE(mode),
            )


_DROP_IN_ALLOWED_TOP_LEVEL_KEYS = frozenset({"sites"})
_DROP_IN_FORBIDDEN_SITE_KEYS = frozenset({"api_token", "personal_token"})


class DropInSitesSource:
    """Reads every ``*.toml`` file in a drop-in directory, lexically sorted.

    Meant for another tool (a dashboard, an installer) to add sites without
    editing the user's own ``config.toml`` -- so unlike ``TomlFileConfigSource``
    it accepts no ``[defaults]``/``[upstream]`` and no literal secrets (only
    ``config.toml`` itself, which the user controls directly, is trusted with
    those). No permission warning: a drop-in file can never hold a token, so
    there's nothing sensitive to warn about.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.name = f"drop-in:{directory}"

    def load(self) -> RawConfig:
        if not self.directory.is_dir():
            return _empty_raw_config()

        sites: dict[str, dict[str, Any]] = {}
        sources: dict[str, str] = {}
        for path in sorted(self.directory.glob("*.toml")):
            for name, fields in self._load_file(path).items():
                # Later files win per key, not per whole entry -- a second
                # drop-in file can override just one field of a site an
                # earlier drop-in file already defined, same as merge_sources.
                sites.setdefault(name, {}).update(fields)
                sources[name] = str(path)

        return {"defaults": {}, "upstream": {}, "sites": sites, "sources": sources}

    def _load_file(self, path: Path) -> dict[str, dict[str, Any]]:
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"drop-in file {path}: invalid TOML: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(f"drop-in file {path}: file is not valid UTF-8: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"drop-in file {path}: could not read file ({exc.__class__.__name__})") from exc

        extra_keys = set(data) - _DROP_IN_ALLOWED_TOP_LEVEL_KEYS
        if extra_keys:
            raise ConfigError(
                f"drop-in file {path}: only [[sites]] is allowed here, found "
                f"top-level key(s): {', '.join(sorted(extra_keys))}"
            )

        raw_sites = data.get("sites", [])
        if not isinstance(raw_sites, list):
            raise ConfigError(
                f"drop-in file {path}: 'sites' must be an array of tables ([[sites]]), not a table"
            )

        sites: dict[str, dict[str, Any]] = {}
        for entry in raw_sites:
            if not isinstance(entry, dict):
                raise ConfigError(f"drop-in file {path}: each [[sites]] entry must be a table")
            name = entry.get("name")
            if not name or not isinstance(name, str):
                raise ConfigError(
                    f"drop-in file {path}: a [[sites]] entry is missing the required 'name' field"
                )
            if name in sites:
                raise ConfigError(f"drop-in file {path}: duplicate [[sites]] entry for name '{name}'")
            forbidden = _DROP_IN_FORBIDDEN_SITE_KEYS & set(entry)
            if forbidden:
                raise ConfigError(
                    f"drop-in file {path}: site '{name}' may not set a literal secret "
                    f"({', '.join(sorted(forbidden))}); use the '_env' form instead "
                    "(api_token_env / personal_token_env)"
                )
            sites[name] = dict(entry)
        return sites


_SUFFIX_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("_API_TOKEN_ENV", "api_token_env", "str"),
    ("_API_TOKEN", "api_token", "str"),
    ("_PERSONAL_TOKEN_ENV", "personal_token_env", "str"),
    ("_PERSONAL_TOKEN", "personal_token", "str"),
    ("_URL", "url", "str"),
    ("_KEY_PREFIXES", "key_prefixes", "list"),
    ("_USERNAME", "username", "str"),
    ("_READ_ONLY", "read_only", "bool"),
    ("_ENABLED_TOOLS", "enabled_tools", "list"),
    ("_PROJECTS_FILTER", "projects_filter", "list"),
)
# Sorted longest-suffix-first so a future field whose suffix is a substring of
# another's can never be matched by the wrong (shorter) entry.
_SUFFIXES_BY_LENGTH = tuple(sorted(_SUFFIX_FIELDS, key=lambda item: len(item[0]), reverse=True))

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class EnvOverlaySource:
    """Reads ``MCP_ATLASSIAN_BETTER_SITE_<NAME>_<FIELD>`` variables as per-site overrides.

    ``<NAME>`` becomes the site name, lowercased. Because environment variable
    names can't contain a dash, a site name containing ``-`` (only ``[a-z0-9_-]``
    is allowed by the config's own naming rule) cannot be targeted this way —
    use ``_`` in the site name, or set the value in the TOML file instead.
    """

    PREFIX = "MCP_ATLASSIAN_BETTER_SITE_"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.name = "env"
        self._environ: Mapping[str, str] = environ if environ is not None else os.environ

    def load(self) -> RawConfig:
        sites: dict[str, dict[str, Any]] = {}
        for key, value in self._environ.items():
            if not key.startswith(self.PREFIX):
                continue
            rest = key[len(self.PREFIX) :]
            for suffix, field, kind in _SUFFIXES_BY_LENGTH:
                if not rest.endswith(suffix):
                    continue
                site_name = rest[: -len(suffix)].lower()
                if not site_name:
                    break
                sites.setdefault(site_name, {})[field] = _parse_env_value(value, kind, key)
                break
        # No provenance: an env overlay only ever patches fields onto a site
        # another source already introduced, so it must never claim ownership
        # of that site in merge_sources' "sources" map.
        return {"defaults": {}, "upstream": {}, "sites": sites, "sources": {}}


def _parse_env_value(value: str, kind: str, var_name: str) -> Any:
    if kind == "list":
        return [item.strip() for item in value.split(",") if item.strip()]
    if kind == "bool":
        lowered = value.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
        raise ConfigError(f"environment variable {var_name}: {value!r} is not a recognized boolean")
    return value


def merge_sources(*raw_configs: RawConfig) -> RawConfig:
    """Merges raw configs in order; later sources win, field by field.

    ``defaults`` and ``upstream`` are merged as flat dicts (a later source's
    keys overwrite matching keys). ``sites`` is merged per site name, and
    within a site, field by field — so an env overlay can override just one
    field of a site defined in the TOML file without restating the rest.
    ``sources`` (site name -> the file/label that last defined that site) is
    merged the same last-wins way, so provenance follows whichever source
    actually won each site.
    """
    merged = _empty_raw_config()
    for raw in raw_configs:
        merged["defaults"].update(raw["defaults"])
        merged["upstream"].update(raw["upstream"])
        for site_name, fields in raw["sites"].items():
            merged["sites"].setdefault(site_name, {}).update(fields)
        merged["sources"].update(raw.get("sources", {}))
    return merged
