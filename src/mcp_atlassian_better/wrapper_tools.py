"""Tools this wrapper implements itself rather than mirroring from a child:
``jira_sites``, the three attachment tools (M3), and ``jira_delete_comment``
(M6) -- all of which talk to Jira Cloud REST v3 directly via
``attachments.AttachmentClientRegistry`` rather than going through an
upstream child -- see that module's docstring for why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import mcp_types
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool

from mcp_atlassian_better.attachments import AttachmentClientRegistry
from mcp_atlassian_better.children import ChildManager
from mcp_atlassian_better.errors import SiteResolutionError
from mcp_atlassian_better.mirror import LateMirror
from mcp_atlassian_better.model import Defaults
from mcp_atlassian_better.registry import SiteRegistry, SiteResolution, resolve_site
from mcp_atlassian_better.site_policy import enforce_site_policy
from mcp_atlassian_better.tools_meta import COMMENT_ID_RE, ISSUE_KEY_RE, shorten_for_error

# Comfortably above any real Jira key (longest observed project keys are a
# handful of characters) but small enough to make a pathological input (e.g.
# thousands of digits) a cheap, obvious rejection rather than a large string
# threaded through logging/URL-building.
_MAX_ISSUE_KEY_LEN = 255
_MAX_COMMENT_ID_LEN = 32


def _shape_entry(entry: dict[str, str]) -> dict[str, str]:
    """Passes a `_DownloadEntry.as_dict()` result through to the tool result,
    keeping only the keys it actually has: `filename` for an entry describing
    a real attachment, `selector` (e.g. `"id:999"`) for an unmatched selector
    that was never a real filename to begin with."""
    shaped = {"reason": entry["reason"]}
    if "filename" in entry:
        shaped["filename"] = entry["filename"]
    if "selector" in entry:
        shaped["selector"] = entry["selector"]
    return shaped


def _resolve_or_raise(
    registry: SiteRegistry, issue_key: str, site: str | None, tool_name: str
) -> SiteResolution:
    try:
        return resolve_site(registry, {"issue_key": issue_key}, explicit=site, tool_name=tool_name)
    except SiteResolutionError as exc:
        raise ToolError(str(exc)) from exc


def _validate_comment_id(comment_id: str, site_name: str) -> str:
    """Jira's DELETE endpoint takes ``comment_id`` straight into the URL
    path, so an unvalidated value (a slash, ``..``, a query string, or a
    trailing newline) would reshape the request rather than simply fail as
    an unknown comment id."""
    # COMMENT_ID_RE has no upper bound on the digit run; Jira ids are far
    # shorter than this, so the cap only stops absurd input reaching the path.
    if len(comment_id) > _MAX_COMMENT_ID_LEN or not COMMENT_ID_RE.fullmatch(comment_id):
        raise ToolError(
            f"[site={site_name}] jira_delete_comment: 'comment_id' must be a Jira comment id "
            f"(digits only), got {shorten_for_error(comment_id)}"
        )
    return comment_id


def _validate_issue_key(issue_key: str, site_name: str, tool_name: str) -> str:
    """Every wrapper-owned REST tool takes ``issue_key`` straight into the
    URL path, so an unvalidated value (a slash, a fragment, ``..``, a query
    string) would reshape the request onto a different endpoint or a
    different issue entirely rather than simply fail as an unknown key --
    site resolution alone does not guard this: an explicit ``site`` argument
    skips routing altogether, and a site with no ``projects_filter``
    configured never runs ``enforce_site_policy``'s own key check either.

    Returns the normalized (stripped, uppercased) key -- callers must use
    THIS value for both the client call and the returned dict, not the raw
    argument, so a lowercase key like ``acme-1`` keeps working instead of
    being rejected."""
    candidate = issue_key.strip().upper()
    # ISSUE_KEY_RE has no upper bound on the digit run, so a bare length cap
    # is the only thing stopping e.g. "CAP-" + "9" * 5000 -- a well-shaped
    # but absurd key -- from reaching the REST path.
    if len(candidate) > _MAX_ISSUE_KEY_LEN or not ISSUE_KEY_RE.fullmatch(candidate):
        raise ToolError(
            f"[site={site_name}] {tool_name}: 'issue_key' must be a Jira issue key like "
            f"PROJ-123, got {shorten_for_error(issue_key)}"
        )
    return candidate


def build_jira_sites_tool(
    manager: ChildManager, defaults: Defaults, *, late_mirror: LateMirror | None = None
) -> Tool:
    """Per-site health, never credentials: name, host, prefixes, read_only,
    state, error, last_error/last_error_at, log path, the FastMCP library
    version each child reports, and whether that site was the tool-discovery
    source. Also carries the ONE shared ``upstream_version`` (mcp-atlassian's
    own version, probed once at startup -- every site launches the same
    command) and, when no configured site is currently healthy, a top-level
    ``note`` explaining that no child could be reached.

    Also drives recovery: every call attempts ``ChildManager.recover_failed_sites()``
    first (a no-op for a site still within its cooldown, or already
    healthy), then, if ``late_mirror`` is given, ``LateMirror.after_recovery``
    -- the only way a server that started with EVERY site down ever gets a
    chance to mirror real tools once one comes back (see ``LateMirror``'s
    docstring). Both are no-ops once nothing is failed / tools are already
    mirrored, so a healthy server pays only the cost of ``manager.health()``.

    Both steps run inside ONE ``anyio.move_on_after(defaults.health_recovery_budget_seconds)``
    scope, so this call's total added latency is bounded by that budget, not
    by either step's own (much longer) internal timeout --
    ``recover_failed_sites``'s spawned attempt and ``after_recovery``'s
    ``discover_tools()`` call both keep running in the background past the
    budget (see their own docstrings) and are picked up by a later
    ``jira_sites`` call; only THIS call's wait for them is cut short. When the
    budget expires mid-``after_recovery``, ``late_mirror`` is left unmirrored
    (``_mirrored`` stays ``False``) for a later call to retry, and the
    response's ``note`` says so instead of claiming anything was mirrored.
    """

    async def jira_sites(ctx: Context) -> dict[str, Any]:
        recovery_note: str | None = None
        with anyio.move_on_after(defaults.health_recovery_budget_seconds) as scope:
            await manager.recover_failed_sites()
            if late_mirror is not None:
                recovery_note = await late_mirror.after_recovery(ctx)
        if scope.cancelled_caught and late_mirror is not None and recovery_note is None:
            recovery_note = "tool mirroring pending; call jira_sites again"

        sites = manager.health()
        payload: dict[str, Any] = {
            "sites": sites,
            "upstream_version": manager.upstream_version(),
        }
        notes: list[str] = []
        healthy = [site for site in sites if site["state"] == "healthy"]
        if not healthy:
            notes.append(
                "no configured site is currently healthy; tool discovery could not run "
                "against any child. Recovery is attempted automatically on every jira_sites "
                "call once a failed site's cooldown has elapsed -- see each site's "
                "'error'/'next_retry_at' above."
            )
        elif all(site["read_only"] or site["enabled_tools_restricted"] for site in healthy):
            # Distinct from the no-healthy-site case above: every child IS
            # reachable, it's just that none of them is allowed to serve a
            # write tool -- so a write tool call fails with a bare "Unknown
            # tool" (fastmcp has no catch-all to shape that into a clearer
            # error) rather than the usual "[site=x] ... read_only" hint.
            notes.append(
                "every healthy site is read_only or has enabled_tools configured; write tools "
                "are not mirrored, so calling one by name fails with a bare 'Unknown tool' rather "
                "than a [site=] error. See each site's 'read_only'/'enabled_tools_restricted' above."
            )
        if recovery_note is not None:
            notes.append(recovery_note)
        if notes:
            payload["note"] = " ".join(notes)
        return payload

    return Tool.from_function(jira_sites, name="jira_sites")


def build_attachment_tools(
    registry: SiteRegistry, attachment_clients: AttachmentClientRegistry
) -> list[Tool]:
    """The three disk-based attachment tools. Each resolves its site the same
    way a mirrored tool does (explicit ``site`` argument, else inferred from
    ``issue_key``'s prefix) via the shared ``resolve_site``."""

    async def jira_list_attachments(issue_key: str, site: str | None = None) -> dict[str, Any]:
        resolution = _resolve_or_raise(registry, issue_key, site, "jira_list_attachments")
        enforce_site_policy(resolution.site, "jira_list_attachments", is_write=False, issue_key=issue_key)
        validated_key = _validate_issue_key(issue_key, resolution.site.name, "jira_list_attachments")
        client = attachment_clients.get(resolution.site.name)
        attachments = await client.list_attachments(validated_key)
        return {
            "site": resolution.site.name,
            "issue_key": validated_key,
            "attachments": [
                {
                    "id": a.id,
                    "filename": a.filename,
                    "size": a.size,
                    "mime_type": a.mime_type,
                    "created": a.created,
                    "author": a.author,
                }
                for a in attachments
            ],
        }

    async def jira_download_attachments(
        issue_key: str,
        target_dir: str,
        site: str | None = None,
        filenames: list[str] | None = None,
        attachment_ids: list[str] | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        resolution = _resolve_or_raise(registry, issue_key, site, "jira_download_attachments")
        enforce_site_policy(resolution.site, "jira_download_attachments", is_write=False, issue_key=issue_key)
        validated_key = _validate_issue_key(issue_key, resolution.site.name, "jira_download_attachments")
        client = attachment_clients.get(resolution.site.name)
        downloaded, entries = await client.download(
            validated_key,
            Path(target_dir),
            filenames=filenames,
            attachment_ids=attachment_ids,
            overwrite=overwrite,
        )
        return {
            "site": resolution.site.name,
            "issue_key": validated_key,
            "target_dir": str(Path(target_dir).expanduser().resolve()),
            "downloaded": [
                {
                    "attachment_id": d.attachment_id,
                    "filename": d.filename,
                    "original_filename": d.original_filename,
                    "path": d.path,
                    "size": d.size,
                    "mime_type": d.mime_type,
                }
                for d in downloaded
            ],
            "skipped": [_shape_entry(e) for e in entries if e["status"] == "skipped"],
            "failed": [_shape_entry(e) for e in entries if e["status"] == "failed"],
        }

    async def jira_upload_attachments(
        issue_key: str, paths: list[str], site: str | None = None
    ) -> dict[str, Any]:
        resolution = _resolve_or_raise(registry, issue_key, site, "jira_upload_attachments")
        enforce_site_policy(resolution.site, "jira_upload_attachments", is_write=True, issue_key=issue_key)
        validated_key = _validate_issue_key(issue_key, resolution.site.name, "jira_upload_attachments")
        resolved_paths = []
        for raw_path in paths:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_file():
                raise ToolError(
                    f"[site={resolution.site.name}] jira_upload_attachments: "
                    f"not an existing regular file: {raw_path}"
                )
            resolved_paths.append(candidate)
        client = attachment_clients.get(resolution.site.name)
        uploaded = await client.upload(validated_key, resolved_paths)
        return {
            "site": resolution.site.name,
            "issue_key": validated_key,
            "uploaded": [
                {"id": a.id, "filename": a.filename, "size": a.size, "mime_type": a.mime_type}
                for a in uploaded
            ],
        }

    return [
        Tool.from_function(
            jira_list_attachments,
            name="jira_list_attachments",
            description=(
                "Lists an issue's attachments: id, filename, size, mime_type, created, author. "
                "Read-only; does not fetch content -- use jira_download_attachments for that. "
                "issue_key must be a Jira issue key like PROJ-123 (case-insensitive)."
            ),
            annotations=mcp_types.ToolAnnotations(read_only_hint=True),
        ),
        Tool.from_function(
            jira_download_attachments,
            name="jira_download_attachments",
            description=(
                "Downloads one or more of an issue's attachments to disk under target_dir. "
                "Writes files to disk and returns their paths; then use your file-reading tool "
                "on the path. Prefer this over any base64 tool. Omit filenames/attachment_ids "
                "to download every attachment (an empty list for either is refused -- omit the "
                "argument instead). Jira Cloud sites only. Use an absolute target_dir "
                "-- a relative one resolves against the server process's own working directory, "
                "not yours (the resolved path is echoed back in the result either way). "
                "issue_key must be a Jira issue key like PROJ-123 (case-insensitive)."
            ),
            # Not read-only despite reading from Jira: it writes to a
            # caller-supplied local path (target_dir), which is exactly the
            # kind of side effect readOnlyHint promises a tool doesn't have.
            # Not destructive (never removes/truncates something the caller
            # didn't ask it to write over) and not idempotent (overwrite=False,
            # the default, can produce a DIFFERENT result on a second call --
            # the id-suffixed fallback name -- once the first call's file
            # exists on disk).
            annotations=mcp_types.ToolAnnotations(
                read_only_hint=False, destructive_hint=False, idempotent_hint=False
            ),
        ),
        Tool.from_function(
            jira_upload_attachments,
            name="jira_upload_attachments",
            description=(
                "Uploads one or more local files as new attachments on an issue. Each path must "
                "already exist as a regular file; use absolute paths -- a relative one resolves "
                "against the server process's own working directory, not yours. Write operation: "
                "refused with a clear error on a site configured read_only = true. issue_key must "
                "be a Jira issue key like PROJ-123 (case-insensitive)."
            ),
            annotations=mcp_types.ToolAnnotations(read_only_hint=False),
        ),
    ]


def build_comment_tools(registry: SiteRegistry, attachment_clients: AttachmentClientRegistry) -> list[Tool]:
    """``jira_delete_comment`` -- upstream ``mcp-atlassian`` has no
    delete-comment tool or library method at all (only add/edit), so this
    goes straight to Jira Cloud REST v3 via the same per-site client the
    attachment tools use."""

    async def jira_delete_comment(issue_key: str, comment_id: str, site: str | None = None) -> dict[str, Any]:
        resolution = _resolve_or_raise(registry, issue_key, site, "jira_delete_comment")
        enforce_site_policy(resolution.site, "jira_delete_comment", is_write=True, issue_key=issue_key)
        validated_key = _validate_issue_key(issue_key, resolution.site.name, "jira_delete_comment")
        validated_id = _validate_comment_id(comment_id, resolution.site.name)
        client = attachment_clients.get(resolution.site.name)
        await client.delete_comment(validated_key, validated_id)
        return {
            "site": resolution.site.name,
            "issue_key": validated_key,
            "comment_id": validated_id,
            "deleted": True,
        }

    return [
        Tool.from_function(
            jira_delete_comment,
            name="jira_delete_comment",
            description=(
                "Permanently deletes ONE comment from an issue. Irreversible -- there is no undo "
                "and no upstream equivalent (mcp-atlassian only supports add/edit). site is "
                "inferred from issue_key's prefix if omitted. Write operation: refused with a "
                "clear error on a site configured read_only = true. Jira Cloud sites only. "
                "issue_key must be a Jira issue key like PROJ-123 (case-insensitive); comment_id "
                "must be digits only."
            ),
            # Not idempotent: a second call with the same arguments errors
            # (comment already gone) rather than repeating the same result.
            annotations=mcp_types.ToolAnnotations(
                read_only_hint=False, destructive_hint=True, idempotent_hint=False
            ),
        )
    ]
