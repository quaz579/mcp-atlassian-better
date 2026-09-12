"""An in-process FastMCP server standing in for one upstream mcp-atlassian
child: echoes back what it received (plus its own site name) so a test can
prove `site` was stripped before forwarding, and see which child answered."""

from __future__ import annotations

from typing import Any

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp_types import ToolAnnotations

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
_DESTRUCTIVE_ONLY = ToolAnnotations(destructive_hint=True)


def make_fake_child(site_name: str, *, leak_secret: str | None = None) -> FastMCP:
    mcp: FastMCP = FastMCP(f"fake-{site_name}")

    @mcp.tool(annotations=_READ_ONLY)
    def jira_get_issue(issue_key: str) -> dict[str, Any]:
        return {"site": site_name, "issue_key": issue_key}

    @mcp.tool(annotations=_READ_ONLY)
    def jira_search(jql: str) -> dict[str, Any]:
        return {"site": site_name, "jql": jql}

    @mcp.tool(annotations=_WRITE)
    def jira_create_issue_link(
        inward_issue_key: str, outward_issue_key: str, link_type: str
    ) -> dict[str, Any]:
        return {
            "site": site_name,
            "inward_issue_key": inward_issue_key,
            "outward_issue_key": outward_issue_key,
        }

    @mcp.tool
    def jira_boom() -> str:
        raise ToolError("boom")

    @mcp.tool
    async def jira_slow() -> str:
        await anyio.sleep(3600)
        return "never"

    # Upstream's base64-in-band tool. Present here purely so a test can prove
    # the parent's tool list never includes it (WRAPPER_OWNED_TOOLS shadows
    # it in favor of the wrapper's own disk-writing version, M3).
    @mcp.tool(annotations=_READ_ONLY)
    def jira_download_attachments(issue_key: str) -> dict[str, Any]:
        return {"site": site_name, "issue_key": issue_key, "base64": "not-really"}

    @mcp.tool(tags={"write"}, annotations=_WRITE)
    def jira_delete_issue(issue_key: str) -> dict[str, Any]:
        return {"site": site_name, "issue_key": issue_key, "deleted": True}

    # Always fails, tagged as a write -- stands in for upstream actually
    # enforcing READ_ONLY_MODE (which this fake doesn't simulate), so a test
    # can exercise the mirror's "configured read_only = true" hint text.
    @mcp.tool(tags={"write"}, annotations=_WRITE)
    def jira_write_blocked(issue_key: str) -> str:
        raise ToolError("write not permitted")

    # Most real upstream write tools (23 of 25 in mcp-atlassian 0.23.1, e.g.
    # add_comment/update_issue/delete_issue) set NO annotations at all, so
    # `annotations` arrives at the mirror as None, not a hint of False.
    @mcp.tool(tags={"write"})
    def jira_add_comment(issue_key: str, body: str) -> str:
        raise ToolError("write not permitted")

    # A tool that sets only destructiveHint (read_only_hint left unset/None,
    # never False) -- must still get the read-only hint appended.
    @mcp.tool(tags={"write"}, annotations=_DESTRUCTIVE_ONLY)
    def jira_destructive_only(issue_key: str) -> str:
        raise ToolError("write not permitted")

    # Mirrors upstream's real jira_get_issue: readOnlyHint=True, raises a
    # normal "not found" error unrelated to read-only enforcement -- the hint
    # must NOT be appended here even on a read_only site.
    @mcp.tool(annotations=_READ_ONLY)
    def jira_get_issue_or_404(issue_key: str) -> dict[str, Any]:
        if issue_key.endswith("99999999"):
            raise ToolError(f"Issue '{issue_key}' does not exist")
        return {"site": site_name, "issue_key": issue_key}

    # Simulates a child echoing a credential back in an error body (e.g. a
    # misconfigured upstream logging its own auth header) -- proves the
    # mirror redacts a child `isError` payload, not just its own exception path.
    if leak_secret is not None:

        @mcp.tool
        def jira_leaky() -> str:
            raise ToolError(f"upstream request failed near token {leak_secret}")

    return mcp
