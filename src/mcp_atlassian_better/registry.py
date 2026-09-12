"""Site registry and the site-resolution algorithm for a tool call."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mcp_atlassian_better.errors import (
    AmbiguousSiteError,
    CrossSiteError,
    UnknownPrefixError,
    UnknownSiteError,
)
from mcp_atlassian_better.model import SiteConfig
from mcp_atlassian_better.tools_meta import (
    ISSUE_KEY_ARGS,
    ISSUE_KEY_RE,
    PROJECT_KEY_ARGS,
    PROJECT_KEY_RE,
    PROJECTS_FILTER_ARGS,
    shorten_for_error,
)

_logger = logging.getLogger(__name__)

# A project-key argument may carry an issue key by mistake (e.g. "ACME-1");
# strip a trailing issue-number suffix rather than fail the whole call.
# `[0-9]`, not `\d`, to match ISSUE_KEY_RE's ASCII-only digit shape (tools_meta.py).
_TRAILING_ISSUE_NUMBER_RE = re.compile(r"(-[0-9]+)+$")


class SiteRegistry:
    """Configured sites, indexed by name (case-insensitive) and key prefix."""

    def __init__(self, sites: Sequence[SiteConfig]) -> None:
        self._sites: tuple[SiteConfig, ...] = tuple(sites)
        self._by_name: dict[str, SiteConfig] = {site.name.lower(): site for site in self._sites}
        self._by_prefix: dict[str, SiteConfig] = {}
        for site in self._sites:
            for prefix in site.key_prefixes:
                self._by_prefix[prefix] = site

    @property
    def sites(self) -> tuple[SiteConfig, ...]:
        return self._sites

    def get_by_name(self, name: str) -> SiteConfig | None:
        return self._by_name.get(name.lower())

    def get_by_prefix(self, prefix: str) -> SiteConfig | None:
        return self._by_prefix.get(prefix)

    def prefix_table(self) -> str:
        """Renders e.g. ``acme: ACME, ACMEOPS | beta: BETA`` for error messages."""
        return " | ".join(f"{site.name}: {', '.join(site.key_prefixes)}" for site in self._sites)


@dataclass(frozen=True, slots=True)
class SiteResolution:
    site: SiteConfig
    reason: str


def _split(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.split(",")
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def resolve_site(
    registry: SiteRegistry,
    arguments: Mapping[str, Any],
    *,
    explicit: str | None = None,
    tool_name: str = "",
) -> SiteResolution:
    explicit = explicit.strip() if explicit else explicit
    if explicit:
        site = registry.get_by_name(explicit)
        if site is None:
            names = ", ".join(s.name for s in registry.sites)
            raise UnknownSiteError(f"unknown site {shorten_for_error(explicit)}; configured sites: {names}")
        return SiteResolution(site=site, reason="explicit")

    if len(registry.sites) == 1:
        return SiteResolution(site=registry.sites[0], reason="only configured site")

    matched_sites: dict[str, SiteConfig] = {}
    matched_detail: dict[str, list[str]] = {}
    first_reason: str | None = None
    # Args that had a non-empty value but whose tokens never resolved to a
    # site (e.g. a purely numeric `projects_filter`) — tracked so the
    # "ambiguous" error can say so, instead of claiming no argument was given.
    present_but_unrouted: list[str] = []

    def record(prefix: str, token: str, arg_name: str) -> None:
        nonlocal first_reason
        site = registry.get_by_prefix(prefix)
        if site is None:
            raise UnknownPrefixError(
                f"tool '{tool_name}': unknown project prefix {shorten_for_error(prefix)} "
                f"(from {shorten_for_error(token)} in '{arg_name}'); "
                f"configured prefixes: {registry.prefix_table()}"
            )
        matched_sites[site.name] = site
        matched_detail.setdefault(site.name, []).append(f"{shorten_for_error(token)} in {arg_name}")
        if first_reason is None:
            first_reason = f"inferred from {shorten_for_error(token)} in {arg_name}"

    for arg_name in ISSUE_KEY_ARGS:
        if arg_name not in arguments or arguments[arg_name] is None:
            continue
        had_content = False
        matched = False
        for token in _split(arguments[arg_name]):
            candidate = token.strip().upper()
            if not candidate:
                continue
            had_content = True
            match = ISSUE_KEY_RE.match(candidate)
            if not match:
                continue
            record(match.group(1), candidate, arg_name)
            matched = True
        if had_content and not matched:
            present_but_unrouted.append(arg_name)

    for arg_name in PROJECT_KEY_ARGS:
        if arg_name not in arguments or arguments[arg_name] is None:
            continue
        for token in _split(arguments[arg_name]):
            candidate = token.strip().upper()
            if not candidate:
                continue
            stripped = _TRAILING_ISSUE_NUMBER_RE.sub("", candidate)
            if stripped != candidate:
                _logger.debug(
                    "tool '%s': %s looked like an issue key, not a project key; "
                    "stripping the issue number: %r -> %r",
                    tool_name,
                    arg_name,
                    candidate,
                    stripped,
                )
                candidate = stripped
            record(candidate, candidate, arg_name)

    for arg_name in PROJECTS_FILTER_ARGS:
        if arg_name not in arguments or arguments[arg_name] is None:
            continue
        had_content = False
        matched = False
        for token in _split(arguments[arg_name]):
            candidate = token.strip().upper()
            if not candidate:
                continue
            had_content = True
            if not PROJECT_KEY_RE.match(candidate):
                # e.g. a numeric project id ("10001") — never a routing signal,
                # and not an error: it just doesn't help resolve a site.
                continue
            record(candidate, candidate, arg_name)
            matched = True
        if had_content and not matched:
            present_but_unrouted.append(arg_name)

    if not matched_sites:
        if present_but_unrouted:
            args_detail = ", ".join(present_but_unrouted)
            raise AmbiguousSiteError(
                f"tool '{tool_name}': {args_detail} present but contained no recognizable issue/project "
                f"key (jql is never parsed for site routing); pass 'site' explicitly. "
                f"Configured prefixes: {registry.prefix_table()}"
            )
        raise AmbiguousSiteError(
            f"tool '{tool_name}': no explicit 'site' and no issue/project key argument to infer one from "
            f"(jql is never parsed for site routing); pass 'site' explicitly. "
            f"Configured prefixes: {registry.prefix_table()}"
        )

    if len(matched_sites) > 1:
        detail = ", ".join(f"{name} ({'; '.join(matched_detail[name])})" for name in sorted(matched_sites))
        raise CrossSiteError(f"tool '{tool_name}': arguments reference multiple sites: {detail}")

    (only_site,) = matched_sites.values()
    assert first_reason is not None
    return SiteResolution(site=only_site, reason=first_reason)
