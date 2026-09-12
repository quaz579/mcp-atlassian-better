"""Real end-to-end proof against actually-configured Jira sites -- the real
server process (spawned exactly the way a client like Claude Code spawns it),
real `mcp-atlassian` children, real Jira Cloud REST calls. No mocks, no fakes.

What this proves: every configured site starts and reports healthy via
`jira_sites`; `jira_get_issue`/`jira_list_attachments` work against a real
issue per site. The write round trip (upload -> list -> download -> sha256
compare -> delete) additionally runs only when
`MCP_ATLASSIAN_BETTER_TEST_SANDBOX_ISSUE` is set -- see the module-level skip below.

Never prints a token, a config value, or any HTTP header. `real_config`'s
site credentials are used only to build a `httpx.BasicAuth` for the cleanup
DELETE call; nothing about them is asserted on or logged.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
import mcp_types
import pytest
from fastmcp import Client
from fastmcp.client.transports import ClientTransport

from mcp_atlassian_better.model import AppConfig, SiteConfig
from mcp_atlassian_better.tools_meta import ISSUE_KEY_RE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_ATLASSIAN_BETTER_INTEGRATION") != "1",
        reason="set MCP_ATLASSIAN_BETTER_INTEGRATION=1 to run real-network integration tests",
    ),
    # Must match live_client's own loop_scope="session" (conftest.py) -- a
    # session-scoped async fixture awaited from a per-test event loop (this
    # project's default) hangs forever rather than erroring, since the
    # Client's internal anyio primitives belong to a different loop than the
    # one the test itself runs on.
    pytest.mark.asyncio(loop_scope="session"),
]

_ATTACHMENTS_API = "/rest/api/3/attachment"


def _sandbox_issue_key() -> str | None:
    return os.environ.get("MCP_ATLASSIAN_BETTER_TEST_SANDBOX_ISSUE")


def _text_content(result: mcp_types.CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, mcp_types.TextContent))


def _site_for_issue_key(config: AppConfig, issue_key: str) -> SiteConfig:
    match = ISSUE_KEY_RE.match(issue_key.strip().upper())
    assert match, f"{issue_key!r} does not look like a Jira issue key"
    prefix = match.group(1)
    for site in config.sites:
        if prefix in site.key_prefixes:
            return site
    pytest.fail(f"no configured site owns project prefix {prefix!r} (from {issue_key!r})")


async def test_jira_sites_reports_every_configured_site_healthy(
    live_client: Client[ClientTransport], real_config: AppConfig
) -> None:
    result = await live_client.call_tool_mcp("jira_sites", {})
    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    by_name = {site["name"]: site for site in payload["sites"]}
    assert set(by_name) == {site.name for site in real_config.sites}
    unhealthy = {name: site["error"] for name, site in by_name.items() if site["state"] != "healthy"}
    assert not unhealthy, f"site(s) not healthy: {unhealthy}"


async def test_get_issue_and_list_attachments_on_every_site(
    live_client: Client[ClientTransport], real_config: AppConfig
) -> None:
    """One real jira_search -> jira_get_issue -> jira_list_attachments pass
    per configured site, using that site's first project prefix. A site with
    zero issues in its first project only skips ITS OWN check (recorded in
    `skipped`), rather than failing the whole test for every other site."""
    checked: list[str] = []
    skipped: dict[str, str] = {}

    for site in real_config.sites:
        project = site.key_prefixes[0]
        search_result = await live_client.call_tool_mcp(
            "jira_search",
            {"jql": f"project = {project} order by created desc", "limit": 1, "site": site.name},
        )
        assert search_result.is_error is False, _text_content(search_result)
        payload = json.loads(_text_content(search_result))
        issues = payload.get("issues", [])
        if not issues:
            skipped[site.name] = f"project '{project}' has no issues"
            continue
        issue_key = issues[0]["key"]

        get_result = await live_client.call_tool_mcp("jira_get_issue", {"issue_key": issue_key})
        assert get_result.is_error is False, _text_content(get_result)
        assert get_result.meta is not None
        assert get_result.meta["mcp-atlassian-better/site"] == site.name

        list_result = await live_client.call_tool_mcp("jira_list_attachments", {"issue_key": issue_key})
        assert list_result.is_error is False, _text_content(list_result)
        assert list_result.structured_content is not None
        assert list_result.structured_content["site"] == site.name
        checked.append(site.name)

    if not checked and skipped:
        pytest.skip(f"no configured site had an issue to test against: {skipped}")


@pytest.mark.skipif(
    _sandbox_issue_key() is None,
    reason=(
        "set MCP_ATLASSIAN_BETTER_TEST_SANDBOX_ISSUE to a writable issue key to run the "
        "attachment write round trip"
    ),
)
async def test_attachment_upload_list_download_delete_round_trip(
    live_client: Client[ClientTransport], real_config: AppConfig, tmp_path: Path
) -> None:
    issue_key = _sandbox_issue_key()
    if issue_key is None:
        # Belt: the skipif above is the real guard; this never runs without
        # the env var set, but a write must never happen silently if some
        # future refactor drops that guard.
        raise RuntimeError(
            "refusing to run the attachment write round trip without "
            "MCP_ATLASSIAN_BETTER_TEST_SANDBOX_ISSUE set"
        )

    site = _site_for_issue_key(real_config, issue_key)

    sites_result = await live_client.call_tool_mcp("jira_sites", {})
    site_health = next(s for s in sites_result.structured_content["sites"] if s["name"] == site.name)
    assert site_health["state"] == "healthy", f"site '{site.name}' is not healthy: {site_health['error']}"
    assert site.read_only is False, (
        f"site '{site.name}' is configured read_only = true; "
        f"refusing to run a write test against it. Use a non-read-only sandbox site."
    )

    content = f"mcp-atlassian-better integration test round trip for {issue_key}\n".encode()
    upload_path = tmp_path / "mcp-atlassian-better-test-upload.txt"
    upload_path.write_bytes(content)

    uploaded_ids: list[str] = []
    try:
        upload_result = await live_client.call_tool_mcp(
            "jira_upload_attachments", {"issue_key": issue_key, "paths": [str(upload_path)]}
        )
        assert upload_result.is_error is False, _text_content(upload_result)
        uploaded = upload_result.structured_content["uploaded"]
        assert len(uploaded) == 1
        attachment_id = uploaded[0]["id"]
        uploaded_ids.append(attachment_id)

        list_result = await live_client.call_tool_mcp("jira_list_attachments", {"issue_key": issue_key})
        assert list_result.is_error is False, _text_content(list_result)
        listed_ids = {a["id"] for a in list_result.structured_content["attachments"]}
        assert attachment_id in listed_ids

        download_dir = tmp_path / "downloaded"
        download_result = await live_client.call_tool_mcp(
            "jira_download_attachments",
            {
                "issue_key": issue_key,
                "target_dir": str(download_dir),
                "attachment_ids": [attachment_id],
            },
        )
        assert download_result.is_error is False, _text_content(download_result)
        downloaded = download_result.structured_content["downloaded"]
        assert len(downloaded) == 1
        downloaded_path = Path(downloaded[0]["path"])
        assert hashlib.sha256(downloaded_path.read_bytes()).digest() == hashlib.sha256(content).digest()
    finally:
        # Cleanup runs even if an assertion above failed, so a failed
        # assertion never leaves a stray test attachment behind on a real
        # customer-visible issue.
        for attachment_id in uploaded_ids:
            assert site.api_token is not None, f"site '{site.name}' has no api_token to authenticate DELETE"
            auth = httpx.BasicAuth(site.username or "", site.api_token.get_secret_value())
            async with httpx.AsyncClient(auth=auth) as http_client:
                delete_response = await http_client.delete(f"{site.url}{_ATTACHMENTS_API}/{attachment_id}")
                assert delete_response.status_code in (204, 404), (
                    f"cleanup DELETE for attachment {attachment_id} on {issue_key} returned "
                    f"{delete_response.status_code}; it may still exist on the issue"
                )


@pytest.mark.skipif(
    _sandbox_issue_key() is None,
    reason=(
        "set MCP_ATLASSIAN_BETTER_TEST_SANDBOX_ISSUE to a writable issue key to run the "
        "delete_comment round trip"
    ),
)
async def test_delete_comment_round_trip(
    live_client: Client[ClientTransport], real_config: AppConfig
) -> None:
    issue_key = _sandbox_issue_key()
    if issue_key is None:
        # Belt: the skipif above is the real guard; see the attachment round
        # trip test's identical comment for why this must never run silently.
        raise RuntimeError(
            "refusing to run the delete_comment round trip without "
            "MCP_ATLASSIAN_BETTER_TEST_SANDBOX_ISSUE set"
        )

    site = _site_for_issue_key(real_config, issue_key)

    sites_result = await live_client.call_tool_mcp("jira_sites", {})
    site_health = next(s for s in sites_result.structured_content["sites"] if s["name"] == site.name)
    assert site_health["state"] == "healthy", f"site '{site.name}' is not healthy: {site_health['error']}"
    assert site.read_only is False, (
        f"site '{site.name}' is configured read_only = true; "
        f"refusing to run a write test against it. Use a non-read-only sandbox site."
    )

    add_result = await live_client.call_tool_mcp(
        "jira_add_comment",
        {"issue_key": issue_key, "body": f"mcp-atlassian-better integration test comment for {issue_key}"},
    )
    assert add_result.is_error is False, _text_content(add_result)
    # Upstream's jira_add_comment returns a JSON string of the created
    # comment's own dict (mcp_atlassian.jira.comments.CommentsMixin.add_comment),
    # always carrying the new comment's id at the top level.
    added = json.loads(_text_content(add_result))
    comment_id = str(added["id"])

    deleted = False
    try:
        delete_result = await live_client.call_tool_mcp(
            "jira_delete_comment", {"issue_key": issue_key, "comment_id": comment_id}
        )
        assert delete_result.is_error is False, _text_content(delete_result)
        assert delete_result.structured_content == {
            "site": site.name,
            "issue_key": issue_key.strip().upper(),
            "comment_id": comment_id,
            "deleted": True,
        }
        deleted = True

        second_delete_result = await live_client.call_tool_mcp(
            "jira_delete_comment", {"issue_key": issue_key, "comment_id": comment_id}
        )
        assert second_delete_result.is_error is True
        assert "404" in _text_content(second_delete_result)
    finally:
        # Best-effort cleanup only if the first delete above never ran (an
        # assertion before it failed) -- once it succeeded, the second delete
        # above already proved the comment is gone with a real 404, so a
        # third delete here would just be redundant noise against the live site.
        if not deleted:
            await live_client.call_tool_mcp(
                "jira_delete_comment", {"issue_key": issue_key, "comment_id": comment_id}
            )
