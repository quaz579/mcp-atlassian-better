"""Tool-level tests for the three attachment tools through an in-process
FastMCP parent, proving `site` resolution (explicit and inferred from
`issue_key`) reaches the correct site's `JiraAttachmentClient`."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport

from mcp_atlassian_better.attachments import AttachmentClientRegistry
from mcp_atlassian_better.model import Defaults, SiteConfig
from mcp_atlassian_better.registry import SiteRegistry
from mcp_atlassian_better.secrets import Secret
from mcp_atlassian_better.wrapper_tools import build_attachment_tools


def _site(
    name: str,
    *prefixes: str,
    read_only: bool = False,
    enabled_tools: frozenset[str] | None = None,
    projects_filter: tuple[str, ...] | None = None,
) -> SiteConfig:
    return SiteConfig(
        name=name,
        url=f"https://{name}.atlassian.net",
        key_prefixes=prefixes,
        username="you@example.com",
        api_token=Secret(f"{name}-token"),
        read_only=read_only,
        enabled_tools=enabled_tools,
        projects_filter=projects_filter,
    )


def _server_for(*sites: SiteConfig) -> tuple[SiteRegistry, FastMCP]:
    registry = SiteRegistry(sites)
    attachment_clients = AttachmentClientRegistry(registry.sites, Defaults(), redact=lambda t: t)
    parent = FastMCP("test-parent")
    for tool in build_attachment_tools(registry, attachment_clients):
        parent.add_tool(tool)
    return registry, parent


@pytest.fixture
async def client() -> AsyncGenerator[Client[FastMCPTransport], None]:
    _registry, parent = _server_for(_site("acme", "ACME"), _site("beta", "BETA"))
    async with Client(FastMCPTransport(parent)) as c:
        yield c


@respx.mock
async def test_jira_list_attachments_infers_site_from_issue_key(client: Client[FastMCPTransport]) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "fields": {
                    "attachment": [
                        {
                            "id": "1",
                            "filename": "a.txt",
                            "size": 1,
                            "mimeType": "text/plain",
                            "created": "2026-01-01T00:00:00.000+0000",
                            "author": {"displayName": "Example User"},
                            "content": "https://acme.atlassian.net/rest/api/3/attachment/content/1",
                        }
                    ]
                }
            },
        )
    )

    result = await client.call_tool_mcp("jira_list_attachments", {"issue_key": "ACME-1"})

    assert result.is_error is False
    assert result.structured_content["site"] == "acme"
    assert result.structured_content["attachments"][0]["filename"] == "a.txt"


@respx.mock
async def test_jira_list_attachments_explicit_site_wins(client: Client[FastMCPTransport]) -> None:
    respx.get("https://beta.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(200, json={"fields": {"attachment": []}})
    )

    result = await client.call_tool_mcp("jira_list_attachments", {"issue_key": "ACME-1", "site": "beta"})

    assert result.is_error is False
    assert result.structured_content["site"] == "beta"


async def test_jira_list_attachments_ambiguous_without_site_is_a_tool_error(
    client: Client[FastMCPTransport],
) -> None:
    result = await client.call_tool_mcp("jira_list_attachments", {"issue_key": "ZZZ-1"})
    assert result.is_error is True


@pytest.mark.parametrize(
    "bad_issue_key",
    [
        "ACME-1#x",
        "ACME-1?x=1",
        "ACME-1/comment/1/../../../ACME-2",
        "not-a-key",
        "",
    ],
)
async def test_jira_list_attachments_rejects_invalid_issue_key_before_http(bad_issue_key: str) -> None:
    """A SINGLE configured site -- `resolve_site` short-circuits to "only
    configured site" without ever regex-checking `issue_key`, and there's no
    `projects_filter` configured to trip `enforce_site_policy`'s own key
    check either, so `_validate_issue_key` is the only thing standing
    between a malicious `issue_key` and the REST path built from it."""
    _registry, parent = _server_for(_site("acme", "ACME"))

    with respx.mock:
        route = respx.get(url__regex=r"https://acme\.atlassian\.net/rest/api/3/issue/.*")
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp("jira_list_attachments", {"issue_key": bad_issue_key})

        assert result.is_error is True
        text = result.content[0].text
        assert "issue_key" in text
        assert not route.called


@respx.mock
async def test_jira_list_attachments_normalizes_a_lowercase_padded_issue_key() -> None:
    _registry, parent = _server_for(_site("acme", "ACME"))
    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(200, json={"fields": {"attachment": []}})
    )

    async with Client(FastMCPTransport(parent)) as c:
        result = await c.call_tool_mcp("jira_list_attachments", {"issue_key": " acme-1 "})

    assert result.is_error is False
    assert result.structured_content["issue_key"] == "ACME-1"


async def test_jira_upload_attachments_rejects_invalid_issue_key_before_any_http_call(tmp_path: Path) -> None:
    _registry, parent = _server_for(_site("acme", "ACME"))
    upload_path = tmp_path / "file.txt"
    upload_path.write_text("hello")

    with respx.mock:
        route = respx.post(url__regex=r"https://acme\.atlassian\.net/rest/api/3/issue/.*")
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp(
                "jira_upload_attachments",
                {"issue_key": "ACME-1/../../ACME-2", "paths": [str(upload_path)]},
            )

        assert result.is_error is True
        text = result.content[0].text
        assert "issue_key" in text
        assert not route.called


async def test_jira_download_attachments_rejects_invalid_issue_key_before_http(tmp_path: Path) -> None:
    _registry, parent = _server_for(_site("acme", "ACME"))

    with respx.mock:
        route = respx.get(url__regex=r"https://acme\.atlassian\.net/rest/api/3/issue/.*")
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp(
                "jira_download_attachments",
                {"issue_key": "ACME-1?x=1", "target_dir": str(tmp_path)},
            )

        assert result.is_error is True
        text = result.content[0].text
        assert "issue_key" in text
        assert not route.called


_ATTACHMENT_TOOLS = ("jira_list_attachments", "jira_download_attachments", "jira_upload_attachments")


def _attachment_call_args(tool_name: str, issue_key: str, tmp_path: Path) -> dict[str, object]:
    """Builds a minimally-valid argument set for whichever attachment tool is
    under test, so the shared ASCII-digit/length-cap/explicit-site tests
    below can be parametrized across all three instead of tripled."""
    args: dict[str, object] = {"issue_key": issue_key}
    if tool_name == "jira_download_attachments":
        args["target_dir"] = str(tmp_path)
    elif tool_name == "jira_upload_attachments":
        upload_path = tmp_path / "file.txt"
        upload_path.write_text("hello")
        args["paths"] = [str(upload_path)]
    return args


@pytest.mark.parametrize("tool_name", _ATTACHMENT_TOOLS)
@pytest.mark.parametrize("bad_issue_key", ["CAP-١", "CAP-１", "CAP-߁"])
async def test_attachment_tools_reject_non_ascii_digit_issue_key(
    tool_name: str, bad_issue_key: str, tmp_path: Path
) -> None:
    """Python's `\\d` (unlike ISSUE_KEY_RE's `[0-9]`) also matches non-ASCII
    decimal digits (Arabic-Indic, fullwidth, N'Ko, ...); a single configured
    site means `_validate_issue_key` is the only thing standing between one
    of these and the REST path."""
    _registry, parent = _server_for(_site("acme", "ACME"))

    call_args = _attachment_call_args(tool_name, bad_issue_key, tmp_path)
    with respx.mock:
        route = respx.route(url__regex=r"https://acme\.atlassian\.net/rest/api/3/.*")
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp(tool_name, call_args)

        assert result.is_error is True
        text = result.content[0].text
        assert "issue_key" in text
        assert not route.called


@pytest.mark.parametrize("tool_name", _ATTACHMENT_TOOLS)
@pytest.mark.parametrize("bad_issue_key", ["CAP-١", "CAP-１", "CAP-߁"])
async def test_attachment_tools_non_ascii_digit_issue_key_is_not_routed(
    tool_name: str, bad_issue_key: str, tmp_path: Path
) -> None:
    """Two configured sites, no explicit `site`: with `\\d`, a non-ASCII
    digit run used to match ISSUE_KEY_RE well enough for `resolve_site` to
    treat 'CAP' as a routable prefix; with ASCII-only digits it matches
    neither site's key_prefixes, so resolution itself fails rather than
    silently routing to a real site."""
    _registry, parent = _server_for(_site("acme", "ACME"), _site("beta", "BETA"))
    call_args = _attachment_call_args(tool_name, bad_issue_key, tmp_path)

    with respx.mock:
        route = respx.route(url__regex=r"https://(acme|beta)\.atlassian\.net/rest/api/3/.*")
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp(tool_name, call_args)

        assert result.is_error is True
        assert not route.called


@pytest.mark.parametrize("tool_name", _ATTACHMENT_TOOLS)
async def test_attachment_tools_reject_an_absurdly_long_issue_key(tool_name: str, tmp_path: Path) -> None:
    _registry, parent = _server_for(_site("acme", "ACME"))
    huge_key = "CAP-" + "9" * 5000

    with respx.mock:
        route = respx.route(url__regex=r"https://acme\.atlassian\.net/rest/api/3/.*")
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp(tool_name, _attachment_call_args(tool_name, huge_key, tmp_path))

        assert result.is_error is True
        text = result.content[0].text
        assert "issue_key" in text
        assert not route.called


@pytest.mark.parametrize("tool_name", _ATTACHMENT_TOOLS)
@pytest.mark.parametrize(
    "bad_issue_key",
    ["ACME-1#x", "ACME-1?x=1", "ACME-1/../../ACME-2", "ACME-1/comment/9"],
)
async def test_attachment_tools_explicit_site_still_rejects_hostile_issue_key(
    tool_name: str, bad_issue_key: str, tmp_path: Path
) -> None:
    """An explicit `site` argument skips routing altogether -- so it must not
    also skip `_validate_issue_key`."""
    _registry, parent = _server_for(_site("acme", "ACME"), _site("beta", "BETA"))
    call_args = _attachment_call_args(tool_name, bad_issue_key, tmp_path)
    call_args["site"] = "acme"

    with respx.mock:
        route = respx.route(url__regex=r"https://acme\.atlassian\.net/rest/api/3/.*")
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp(tool_name, call_args)

        assert result.is_error is True
        text = result.content[0].text
        assert "issue_key" in text
        assert not route.called


async def test_jira_download_attachments_annotations_reflect_its_local_write_side_effect(
    client: Client[FastMCPTransport],
) -> None:
    """It writes to a caller-supplied local path, so despite only reading
    from Jira it must not advertise readOnlyHint=True -- and overwrite=False
    (the default) can produce a different result on a second call once the
    first call's file exists, so not idempotentHint either."""
    tools = await client.list_tools()
    download = next(t for t in tools if t.name == "jira_download_attachments")
    assert download.annotations is not None
    assert download.annotations.read_only_hint is False
    assert download.annotations.destructive_hint is False
    assert download.annotations.idempotent_hint is False

    list_tool = next(t for t in tools if t.name == "jira_list_attachments")
    assert list_tool.annotations is not None
    assert list_tool.annotations.read_only_hint is True

    upload = next(t for t in tools if t.name == "jira_upload_attachments")
    assert upload.annotations is not None
    assert upload.annotations.read_only_hint is False


async def test_jira_upload_attachments_rejects_a_missing_path(
    client: Client[FastMCPTransport], tmp_path: Path
) -> None:
    result = await client.call_tool_mcp(
        "jira_upload_attachments",
        {"issue_key": "ACME-1", "paths": [str(tmp_path / "does-not-exist.txt")]},
    )
    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "not an existing regular file" in text


@respx.mock
async def test_jira_download_attachments_writes_to_disk_and_returns_paths(
    client: Client[FastMCPTransport], tmp_path: Path
) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-2", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "fields": {
                    "attachment": [
                        {
                            "id": "5",
                            "filename": "screenshot.png",
                            "size": 3,
                            "mimeType": "image/png",
                            "created": "2026-01-01T00:00:00.000+0000",
                            "author": {"displayName": "Example User"},
                            "content": "https://acme.atlassian.net/rest/api/3/attachment/content/5",
                        }
                    ]
                }
            },
        )
    )
    respx.get("https://acme.atlassian.net/rest/api/3/attachment/content/5").mock(
        return_value=httpx.Response(200, content=b"\x89PN")
    )

    result = await client.call_tool_mcp(
        "jira_download_attachments", {"issue_key": "ACME-2", "target_dir": str(tmp_path)}
    )

    assert result.is_error is False
    payload = result.structured_content
    assert payload["downloaded"][0]["filename"] == "screenshot.png"
    written_path = Path(payload["downloaded"][0]["path"])
    assert written_path.read_bytes() == b"\x89PN"


async def test_upload_refused_on_read_only_site_with_no_http_request_made(tmp_path: Path) -> None:
    _registry, parent = _server_for(_site("acme", "ACME", read_only=True))
    upload_path = tmp_path / "file.txt"
    upload_path.write_text("hello")

    with respx.mock:
        route = respx.post("https://acme.atlassian.net/rest/api/3/issue/ACME-1/attachments")
        c: Client[FastMCPTransport]
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp(
                "jira_upload_attachments", {"issue_key": "ACME-1", "paths": [str(upload_path)]}
            )

        assert result.is_error is True
        text = result.content[0].text  # type: ignore[union-attr]
        assert "read_only" in text
        assert not route.called


async def test_list_and_download_still_work_on_a_read_only_site(tmp_path: Path) -> None:
    _registry, parent = _server_for(_site("acme", "ACME", read_only=True))

    with respx.mock:
        respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
            return_value=httpx.Response(200, json={"fields": {"attachment": []}})
        )
        async with Client(FastMCPTransport(parent)) as c:
            list_result = await c.call_tool_mcp("jira_list_attachments", {"issue_key": "ACME-1"})
            download_result = await c.call_tool_mcp(
                "jira_download_attachments", {"issue_key": "ACME-1", "target_dir": str(tmp_path)}
            )

        assert list_result.is_error is False
        assert download_result.is_error is False


async def test_upload_refused_when_enabled_tools_excludes_it(tmp_path: Path) -> None:
    _registry, parent = _server_for(_site("acme", "ACME", enabled_tools=frozenset({"jira_list_attachments"})))
    upload_path = tmp_path / "file.txt"
    upload_path.write_text("hello")

    with respx.mock:
        route = respx.post("https://acme.atlassian.net/rest/api/3/issue/ACME-1/attachments")
        c: Client[FastMCPTransport]
        async with Client(FastMCPTransport(parent)) as c:
            result = await c.call_tool_mcp(
                "jira_upload_attachments", {"issue_key": "ACME-1", "paths": [str(upload_path)]}
            )

        assert result.is_error is True
        text = result.content[0].text  # type: ignore[union-attr]
        assert "enabled_tools" in text
        assert not route.called


async def test_projects_filter_mismatch_is_a_tool_error() -> None:
    _registry, parent = _server_for(_site("acme", "ACME", "ZZZ", projects_filter=("ZZZ",)))

    c: Client[FastMCPTransport]
    async with Client(FastMCPTransport(parent)) as c:
        result = await c.call_tool_mcp("jira_list_attachments", {"issue_key": "ACME-1"})

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "projects_filter" in text


async def test_projects_filter_numeric_issue_id_is_refused_not_silently_skipped() -> None:
    """Jira also accepts a numeric issue id in place of a key; ISSUE_KEY_RE
    never matches one, so without an explicit refusal this check would
    simply be skipped -- letting a numeric id bypass projects_filter
    entirely (the identifier is never even resolved to a real project)."""
    _registry, parent = _server_for(_site("acme", "ACME", projects_filter=("ACME",)))

    c: Client[FastMCPTransport]
    async with Client(FastMCPTransport(parent)) as c:
        result = await c.call_tool_mcp("jira_list_attachments", {"issue_key": "81498", "site": "acme"})

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "projects_filter" in text
    assert "81498" in text


@respx.mock
async def test_projects_filter_allows_a_matching_real_issue_key() -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(200, json={"fields": {"attachment": []}})
    )
    _registry, parent = _server_for(_site("acme", "ACME", projects_filter=("ACME",)))

    c: Client[FastMCPTransport]
    async with Client(FastMCPTransport(parent)) as c:
        result = await c.call_tool_mcp("jira_list_attachments", {"issue_key": "ACME-1", "site": "acme"})

    assert result.is_error is False
