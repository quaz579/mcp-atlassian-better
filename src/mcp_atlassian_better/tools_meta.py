"""Metadata about upstream mcp-atlassian's Jira tools used for site routing.

Tool names verified against sooperset/mcp-atlassian at tag ``v0.23.1``
(``src/mcp_atlassian/servers/jira.py``, mounted under the ``jira`` namespace
in ``servers/main.py`` — every tool's wire name is ``jira_<function_name>``).
"""

from __future__ import annotations

import re

# Arguments that may carry one or more issue keys (str, comma-separated str,
# or list). Never includes "jql": free-text JQL is never parsed for a site.
ISSUE_KEY_ARGS: tuple[str, ...] = (
    "issue_key",
    "issue_keys",
    "epic_key",
    "parent",
    "inward_issue_key",
    "outward_issue_key",
    "issue_ids_or_keys",
)

# Arguments that carry a bare project key (no issue number suffix).
PROJECT_KEY_ARGS: tuple[str, ...] = (
    "project_key",
    "target_project_key",
)

# jira_search's own project-scoping argument (comma-separated). It may mix
# real project keys with numeric project ids; only tokens that look like a
# project key are used for routing (see PROJECT_KEY_RE) — a numeric id is
# silently skipped rather than treated as an unknown prefix, since it never
# carries one.
PROJECTS_FILTER_ARGS: tuple[str, ...] = ("projects_filter",)

# Arguments that must never be inspected for site-routing purposes, even
# though they can contain text that looks like an issue key.
NEVER_PARSED: frozenset[str] = frozenset({"jql"})

# Default display length for shorten_for_error: bounds an echoed caller value
# in error text without needing every call site to pick its own limit.
_DEFAULT_ERROR_SHOW_LEN = 80


def shorten_for_error(value: str, limit: int = _DEFAULT_ERROR_SHOW_LEN) -> str:
    """Truncates and reprs a raw, caller-supplied string before it's embedded
    in an error message. Several call sites echo an `issue_key` (or similar)
    BEFORE it has passed length/shape validation -- site resolution and
    `enforce_site_policy` both run first -- so without this, a pathological
    value (thousands of characters, an embedded newline) would reach the
    message untruncated and unescaped."""
    shown = value if len(value) <= limit else f"{value[:limit]}...({len(value)} chars)"
    return repr(shown)


# `[0-9]`, not `\d` -- Python's `\d` also matches non-ASCII decimal digits
# (Arabic-indic, fullwidth, etc.), which would otherwise reach the REST URL
# percent-encoded instead of being refused as an invalid key.
ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)-[0-9]+(?:-[0-9]+)*$")

# Shape of a bare project key (also used to validate a configured key_prefix).
# A single letter is a valid Jira project key upstream, so this allows one.
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Jira REST comment ids are purely numeric. Shared by wrapper_tools's
# argument validator and JiraAttachmentClient's own defense-in-depth check,
# so the two can never drift apart -- always used with `.fullmatch()`, never
# `.match()` (`$` alone matches before a trailing newline, which `.match()`
# would let through as if it were a bare digit string).
COMMENT_ID_RE = re.compile(r"^[0-9]+$")

_ROUTABLE_ARGS = frozenset(ISSUE_KEY_ARGS) | frozenset(PROJECT_KEY_ARGS) | frozenset(PROJECTS_FILTER_ARGS)
assert not (NEVER_PARSED & _ROUTABLE_ARGS), "NEVER_PARSED must never overlap a routable argument name"

# The curated subset of upstream's Jira tools this server mirrors by default
# (toolset_preset = "curated"). Excludes agile board/sprint tools (out of
# scope for v1) and anything not read/write core issue, search, comment,
# transition, user, link, worklog, or basic project functionality.
CURATED_TOOLS: frozenset[str] = frozenset(
    {
        # issues
        "jira_get_issue",
        "jira_create_issue",
        "jira_batch_create_issues",
        "jira_batch_get_changelogs",
        "jira_update_issue",
        "jira_assign_issue",
        "jira_delete_issue",
        "jira_move_issue",
        # search / fields
        "jira_search",
        "jira_search_fields",
        "jira_get_field_options",
        "jira_get_create_fields",
        "jira_get_project_fields",
        # comments
        "jira_add_comment",
        "jira_edit_comment",
        # transitions
        "jira_get_transitions",
        "jira_transition_issue",
        # attachments (image inline content; disk download is wrapper-owned)
        "jira_get_issue_images",
        "jira_download_attachments",
        # users
        "jira_get_user_profile",
        "jira_search_assignable_users",
        "jira_get_issue_watchers",
        "jira_add_watcher",
        "jira_remove_watcher",
        # links
        "jira_get_link_types",
        "jira_create_issue_link",
        "jira_create_remote_issue_link",
        "jira_remove_issue_link",
        "jira_link_to_epic",
        # worklog
        "jira_get_worklog",
        "jira_add_worklog",
        # projects (basics)
        "jira_get_project_issues",
        "jira_get_project_issue_types",
        "jira_get_project_versions",
        "jira_get_project_components",
        "jira_get_all_projects",
        "jira_search_projects",
    }
)

# Tools this wrapper implements itself (in wrapper_tools.py) instead of
# exposing the upstream child's version. The upstream "jira_download_attachments"
# returns base64 in-band; ours writes to disk. "jira_list_attachments",
# "jira_upload_attachments", and "jira_delete_comment" have no upstream
# equivalent at all -- they're wrapper-only, like "jira_sites". Listing every
# one of these here (even the ones no real child has ever advertised) means a
# future upstream tool collision would be shadowed rather than silently
# double-registered. Mirror logic must exclude these names from
# ENABLED_TOOLS on children and never forward calls to them.
WRAPPER_OWNED_TOOLS: frozenset[str] = frozenset(
    {
        "jira_download_attachments",
        "jira_list_attachments",
        "jira_upload_attachments",
        "jira_delete_comment",
        "jira_sites",
    }
)
