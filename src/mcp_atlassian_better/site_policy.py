"""Shared per-site policy enforcement, called by every wrapper-owned tool AND,
as a belt, by every mirrored (child) tool (``mirror.MultiSiteProxyTool.run``).

A mirrored tool's real enforcement of ``read_only`` and ``projects_filter``
(the static ``JIRA_PROJECTS_FILTER``, distinct from a tool call's own
``projects_filter`` argument used only for site routing) comes entirely from
the upstream child process itself -- ``READ_ONLY_MODE`` and
``JIRA_PROJECTS_FILTER`` are env vars baked into that child at launch (see
``children.build_child_env``). ``mirror.py`` calls this module too, but with
``is_write=False`` (never trips the ``read_only`` check below) and
``issue_key=None`` (never trips the ``projects_filter`` check below) -- on
purpose: pre-emptively refusing a call here based on a tool's advertised
``readOnlyHint`` would risk blocking a legitimate read (verified against
real curated tools, every one sets ``readOnlyHint=True`` explicitly, but the
``toolset_preset = "all"`` case can mirror uncurated tools whose annotations
aren't verified).
``enabled_tools`` is the one policy enforced by BOTH sides: the child's own
``ENABLED_TOOLS`` env var, and this module's check below (reached from
``mirror.py`` regardless of ``is_write``/``issue_key``), so a misbehaving
child that serves a tool outside its configured allowlist is still refused.

A wrapper-owned tool (the three attachment tools; anything added later)
talks to Jira directly and never passes through a child, so without this
module it would silently bypass every per-site policy -- including
``projects_filter``, which only a wrapper-owned tool call actually checks
here (a mirrored call's ``projects_filter`` enforcement is entirely the
child's ``JIRA_PROJECTS_FILTER``). One guard, used everywhere, rather than
several separate checks that could drift out of sync.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from mcp_atlassian_better.model import SiteConfig
from mcp_atlassian_better.tools_meta import ISSUE_KEY_RE, shorten_for_error


def enforce_site_policy(site: SiteConfig, tool_name: str, *, is_write: bool, issue_key: str | None) -> None:
    """Raises ``ToolError`` if ``site``'s configuration forbids this call.

    Checked in order -- whether the site serves this tool at all, then
    whether this specific call is a write against a read-only site, then
    whether the target issue's project is in scope -- so a caller always
    sees the most fundamental reason first rather than a generic refusal.
    """
    if site.enabled_tools is not None and tool_name not in site.enabled_tools:
        raise ToolError(f"[site={site.name}] {tool_name}: tool is not in this site's enabled_tools")

    if is_write and site.read_only:
        raise ToolError(f"[site={site.name}] {tool_name}: site is configured read_only = true")

    if site.projects_filter is not None and issue_key is not None:
        candidate = issue_key.strip().upper()
        match = ISSUE_KEY_RE.match(candidate)
        if not match:
            # Jira also accepts a numeric issue id (e.g. "81498") in place of
            # a key -- ISSUE_KEY_RE never matches one, so without this the
            # projects_filter check below would simply be skipped, letting a
            # numeric id (or any other malformed value) bypass the
            # restriction entirely. Refuse outright rather than resolving it
            # (that would need an extra Jira call just to find out what
            # project it belongs to).
            raise ToolError(
                f"[site={site.name}] {tool_name}: projects_filter is configured for this site; "
                f"use a project issue key (e.g. PROJ-123); got {shorten_for_error(issue_key)}"
            )
        project = match.group(1)
        # `site.projects_filter` is normalized to uppercase at config load
        # (see config._parse_projects_filter), and `candidate` is uppercased
        # above, so this comparison is already case-insensitive.
        if project not in site.projects_filter:
            raise ToolError(
                f"[site={site.name}] {tool_name}: project {shorten_for_error(project)} (from issue "
                f"{shorten_for_error(issue_key)}) is not in this site's projects_filter "
                f"({', '.join(site.projects_filter)})"
            )
