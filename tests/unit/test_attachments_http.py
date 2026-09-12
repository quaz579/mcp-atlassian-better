"""HTTP-level behavior of ``JiraAttachmentClient`` against a respx-mocked Jira
Cloud REST v3, including the cross-host redirect for attachment content and
that a child's error body never lets a credential reach the model."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_atlassian_better.attachments import JiraAttachmentClient
from mcp_atlassian_better.model import SiteConfig
from mcp_atlassian_better.secrets import Secret, redact_text, scrub_urls

_SECRET_TOKEN = "zz-super-secret-api-token-zz"


def _cloud_site(name: str = "acme", *, token: str = _SECRET_TOKEN) -> SiteConfig:
    return SiteConfig(
        name=name,
        url=f"https://{name}.atlassian.net",
        key_prefixes=("ACME",),
        username="you@example.com",
        api_token=Secret(token),
    )


def _dc_site(name: str = "onprem") -> SiteConfig:
    return SiteConfig(
        name=name,
        url=f"https://{name}.example.com",
        key_prefixes=("ONPREM",),
        personal_token=Secret("dc-token"),
    )


def _client(
    site: SiteConfig, http: httpx.AsyncClient, *, max_bytes: int = 10_000_000
) -> JiraAttachmentClient:
    return JiraAttachmentClient(
        site, http, max_bytes=max_bytes, redact=lambda text: redact_text(text, [Secret(_SECRET_TOKEN)])
    )


@pytest.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(auth=httpx.BasicAuth("you@example.com", _SECRET_TOKEN)) as client:
        yield client


@respx.mock
async def test_list_attachments_parses_fields(http_client: httpx.AsyncClient) -> None:
    site = _cloud_site()
    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "fields": {
                    "attachment": [
                        {
                            "id": "10001",
                            "filename": "notes.txt",
                            "size": 42,
                            "mimeType": "text/plain",
                            "created": "2026-09-10T12:00:00.000+0000",
                            "author": {"displayName": "Example User"},
                            "content": "https://acme.atlassian.net/rest/api/3/attachment/content/10001",
                        }
                    ]
                }
            },
        )
    )

    attachments = await _client(site, http_client).list_attachments("ACME-1")

    assert len(attachments) == 1
    a = attachments[0]
    assert a.id == "10001"
    assert a.filename == "notes.txt"
    assert a.size == 42
    assert a.mime_type == "text/plain"
    assert a.created == "2026-09-10T12:00:00.000+0000"
    assert a.author == "Example User"
    assert a.content_url == "https://acme.atlassian.net/rest/api/3/attachment/content/10001"


@respx.mock
async def test_download_follows_cross_host_redirect_and_writes_bytes(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    site = _cloud_site()
    content_url = "https://acme.atlassian.net/rest/api/3/attachment/content/10001"
    cdn_url = "https://media-cdn.example-atlassian-media.net/some-signed-path"

    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "fields": {
                    "attachment": [
                        {
                            "id": "10001",
                            "filename": "notes.txt",
                            "size": 5,
                            "mimeType": "text/plain",
                            "created": "2026-09-10T12:00:00.000+0000",
                            "author": {"displayName": "Example User"},
                            "content": content_url,
                        }
                    ]
                }
            },
        )
    )
    respx.get(content_url).mock(return_value=httpx.Response(302, headers={"location": cdn_url}))
    respx.get(cdn_url).mock(return_value=httpx.Response(200, content=b"hello"))

    downloaded, skipped_or_failed = await _client(site, http_client).download("ACME-1", tmp_path)

    assert skipped_or_failed == []
    assert len(downloaded) == 1
    result = downloaded[0]
    assert result.filename == "notes.txt"
    assert Path(result.path).read_bytes() == b"hello"
    assert result.size == 5

    cdn_request = next(call.request for call in respx.calls if call.request.url == cdn_url)
    assert "authorization" not in {h.lower() for h in cdn_request.headers.keys()}


@respx.mock
async def test_upload_sends_multipart_with_no_check_header(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    site = _cloud_site()
    upload_path = tmp_path / "mcp-atlassian-better-test.txt"
    upload_path.write_text("hello world")

    route = respx.post("https://acme.atlassian.net/rest/api/3/issue/ACME-1/attachments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "20002",
                    "filename": "mcp-atlassian-better-test.txt",
                    "size": 11,
                    "mimeType": "text/plain",
                    "created": "2026-09-10T12:00:00.000+0000",
                    "author": {"displayName": "Example User"},
                    "content": "https://acme.atlassian.net/rest/api/3/attachment/content/20002",
                }
            ],
        )
    )

    uploaded = await _client(site, http_client).upload("ACME-1", [upload_path])

    assert route.called
    sent_request = route.calls.last.request
    assert sent_request.headers["X-Atlassian-Token"] == "no-check"
    assert b'name="file"' in sent_request.content
    assert b"mcp-atlassian-better-test.txt" in sent_request.content
    assert len(uploaded) == 1
    assert uploaded[0].id == "20002"
    assert uploaded[0].filename == "mcp-atlassian-better-test.txt"


async def test_upload_rejects_a_file_over_max_bytes_without_sending_it(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    site = _cloud_site()
    upload_path = tmp_path / "big.bin"
    upload_path.write_bytes(b"x" * 200)

    with respx.mock:
        route = respx.post("https://acme.atlassian.net/rest/api/3/issue/ACME-1/attachments")
        with pytest.raises(ToolError, match="max_bytes"):
            await _client(site, http_client, max_bytes=100).upload("ACME-1", [upload_path])
        assert not route.called


async def test_upload_open_failure_is_redacted(
    http_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _cloud_site()
    upload_path = tmp_path / "notes.txt"
    upload_path.write_text("hello")
    real_open = open

    def flaky_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) == str(upload_path):
            raise OSError(f"permission denied near token {_SECRET_TOKEN}")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).upload("ACME-1", [upload_path])

    message = str(exc_info.value)
    assert _SECRET_TOKEN not in message
    assert "***" in message


@respx.mock
async def test_403_error_carries_jira_messages_and_not_the_token(http_client: httpx.AsyncClient) -> None:
    site = _cloud_site()
    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(
            403, json={"errorMessages": ["You do not have permission to view this issue."]}
        )
    )

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).list_attachments("ACME-1")

    message = str(exc_info.value)
    assert message.startswith("[site=acme] jira_list_attachments: Jira returned 403")
    assert "You do not have permission to view this issue." in message
    assert _SECRET_TOKEN not in message


@respx.mock
async def test_404_issue_raises_tool_error_with_jira_message(http_client: httpx.AsyncClient) -> None:
    site = _cloud_site()
    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-99999", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
    )

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).list_attachments("ACME-99999")

    message = str(exc_info.value)
    assert message.startswith("[site=acme] jira_list_attachments: Jira returned 404")
    assert "Issue does not exist" in message


@respx.mock
async def test_transport_error_during_download_is_redacted_and_stripped_of_url_query(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A transport-level failure's `str()` can itself carry a CDN URL's query
    string (a credential) or a known secret value -- both must be scrubbed
    before the message reaches a `_DownloadEntry`, same as any other tool
    result."""
    site = _cloud_site()
    content_url = "https://acme.atlassian.net/rest/api/3/attachment/content/10001"
    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "fields": {
                    "attachment": [
                        {
                            "id": "10001",
                            "filename": "notes.txt",
                            "size": 5,
                            "mimeType": "text/plain",
                            "created": "2026-09-10T12:00:00.000+0000",
                            "author": {"displayName": "Example User"},
                            "content": content_url,
                        }
                    ]
                }
            },
        )
    )

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"connection to {content_url}?token=SECRET failed near token {_SECRET_TOKEN}"
        )

    respx.get(content_url).mock(side_effect=boom)

    client = JiraAttachmentClient(
        site,
        http_client,
        max_bytes=10_000_000,
        redact=lambda text: scrub_urls(redact_text(text, [Secret(_SECRET_TOKEN)])),
    )
    downloaded, entries = await client.download("ACME-1", tmp_path)

    assert downloaded == []
    assert entries[0]["status"] == "failed"
    assert _SECRET_TOKEN not in entries[0]["reason"]
    assert "token=SECRET" not in entries[0]["reason"]
    assert "?***" in entries[0]["reason"]


@respx.mock
async def test_transport_error_during_list_is_shaped_as_a_tool_error(http_client: httpx.AsyncClient) -> None:
    """A connection failure on the LIST phase itself (as opposed to the
    per-attachment content download, covered above) used to escape unshaped
    -- no ``[site=]`` prefix, no tool name, no redaction."""
    site = _cloud_site()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection failed near token {_SECRET_TOKEN}")

    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        side_effect=boom
    )

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).list_attachments("ACME-1")

    message = str(exc_info.value)
    assert message.startswith("[site=acme] jira_list_attachments:")
    assert _SECRET_TOKEN not in message
    assert "***" in message


@respx.mock
async def test_transport_error_with_no_message_still_shows_exception_class(
    http_client: httpx.AsyncClient,
) -> None:
    """A bare `httpx.ConnectError()` stringifies to "" -- without a fallback
    the shaped message would read "ConnectError: " with nothing after the
    colon, looking like a truncated message rather than one that never
    existed."""
    site = _cloud_site()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("")

    respx.get("https://acme.atlassian.net/rest/api/3/issue/ACME-1", params={"fields": "attachment"}).mock(
        side_effect=boom
    )

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).list_attachments("ACME-1")

    message = str(exc_info.value)
    assert "ConnectError: (no detail)" in message


@respx.mock
async def test_transport_error_during_upload_post_is_shaped_as_a_tool_error(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Same fix as the LIST phase above, for the UPLOAD POST."""
    site = _cloud_site()
    upload_path = tmp_path / "notes.txt"
    upload_path.write_text("hello")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection failed near token {_SECRET_TOKEN}")

    respx.post("https://acme.atlassian.net/rest/api/3/issue/ACME-1/attachments").mock(side_effect=boom)

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).upload("ACME-1", [upload_path])

    message = str(exc_info.value)
    assert message.startswith("[site=acme] jira_upload_attachments:")
    assert _SECRET_TOKEN not in message
    assert "***" in message


async def test_dc_site_refuses_all_three_operations(http_client: httpx.AsyncClient, tmp_path: Path) -> None:
    site = _dc_site()
    client = _client(site, http_client)

    with pytest.raises(ToolError, match="Jira Cloud sites only in this version"):
        await client.list_attachments("ONPREM-1")
    with pytest.raises(ToolError, match="Jira Cloud sites only in this version"):
        await client.download("ONPREM-1", tmp_path)
    with pytest.raises(ToolError, match="Jira Cloud sites only in this version"):
        await client.upload("ONPREM-1", [])


@respx.mock
async def test_delete_comment_success_raises_nothing_on_204(http_client: httpx.AsyncClient) -> None:
    site = _cloud_site()
    route = respx.delete("https://acme.atlassian.net/rest/api/3/issue/ACME-1/comment/10050").mock(
        return_value=httpx.Response(204)
    )

    await _client(site, http_client).delete_comment("ACME-1", "10050")

    assert route.called


@respx.mock
async def test_delete_comment_403_carries_jira_messages_and_not_the_token(
    http_client: httpx.AsyncClient,
) -> None:
    site = _cloud_site()
    respx.delete("https://acme.atlassian.net/rest/api/3/issue/ACME-1/comment/10050").mock(
        return_value=httpx.Response(
            403, json={"errorMessages": ["You do not have permission to delete this comment."]}
        )
    )

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).delete_comment("ACME-1", "10050")

    message = str(exc_info.value)
    assert message.startswith("[site=acme] jira_delete_comment: Jira returned 403")
    assert "You do not have permission to delete this comment." in message
    assert _SECRET_TOKEN not in message


@respx.mock
async def test_delete_comment_404_raises_tool_error_with_jira_message(http_client: httpx.AsyncClient) -> None:
    site = _cloud_site()
    respx.delete("https://acme.atlassian.net/rest/api/3/issue/ACME-1/comment/99999").mock(
        return_value=httpx.Response(404, json={"errorMessages": ["Comment does not exist"]})
    )

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).delete_comment("ACME-1", "99999")

    message = str(exc_info.value)
    assert message.startswith("[site=acme] jira_delete_comment: Jira returned 404")
    assert "Comment does not exist" in message


@respx.mock
async def test_delete_comment_transport_error_is_shaped_as_a_tool_error(
    http_client: httpx.AsyncClient,
) -> None:
    site = _cloud_site()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection failed near token {_SECRET_TOKEN}")

    respx.delete("https://acme.atlassian.net/rest/api/3/issue/ACME-1/comment/10050").mock(side_effect=boom)

    with pytest.raises(ToolError) as exc_info:
        await _client(site, http_client).delete_comment("ACME-1", "10050")

    message = str(exc_info.value)
    assert message.startswith("[site=acme] jira_delete_comment:")
    assert _SECRET_TOKEN not in message
    assert "***" in message


async def test_dc_site_refuses_delete_comment(http_client: httpx.AsyncClient) -> None:
    site = _dc_site()
    with pytest.raises(ToolError, match="Jira Cloud sites only in this version"):
        await _client(site, http_client).delete_comment("ONPREM-1", "10050")


# Belt-and-braces: `wrapper_tools._validate_issue_key`/`_validate_comment_id`
# already refuse these before the client is ever called, but a future caller
# that skips that layer must not be able to build a request from an
# unvalidated `issue_key`/`comment_id` either -- these call `JiraAttachmentClient`
# directly, the way such a caller would.


async def test_list_attachments_rejects_invalid_issue_key_without_the_wrapper(
    http_client: httpx.AsyncClient,
) -> None:
    site = _cloud_site()
    with respx.mock:
        route = respx.get(url__regex=r"https://acme\.atlassian\.net/rest/api/3/issue/.*")
        with pytest.raises(ToolError) as exc_info:
            await _client(site, http_client).list_attachments("ACME-1#x")
        assert not route.called
    assert "[site=acme] jira_list_attachments:" in str(exc_info.value)
    assert "issue_key" in str(exc_info.value)


async def test_upload_rejects_invalid_issue_key_without_the_wrapper(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    site = _cloud_site()
    upload_path = tmp_path / "notes.txt"
    upload_path.write_text("hello")
    with respx.mock:
        route = respx.post(url__regex=r"https://acme\.atlassian\.net/rest/api/3/issue/.*")
        with pytest.raises(ToolError) as exc_info:
            await _client(site, http_client).upload("ACME-1/../../ACME-2", [upload_path])
        assert not route.called
    assert "[site=acme] jira_upload_attachments:" in str(exc_info.value)
    assert "issue_key" in str(exc_info.value)


async def test_delete_comment_rejects_invalid_issue_key_without_the_wrapper(
    http_client: httpx.AsyncClient,
) -> None:
    site = _cloud_site()
    with respx.mock:
        route = respx.delete(url__regex=r"https://acme\.atlassian\.net/rest/api/3/issue/.*")
        with pytest.raises(ToolError) as exc_info:
            await _client(site, http_client).delete_comment("ACME-1?x=1", "10050")
        assert not route.called
    assert "[site=acme] jira_delete_comment:" in str(exc_info.value)
    assert "issue_key" in str(exc_info.value)


async def test_delete_comment_rejects_invalid_comment_id_without_the_wrapper(
    http_client: httpx.AsyncClient,
) -> None:
    site = _cloud_site()
    with respx.mock:
        route = respx.delete(url__regex=r"https://acme\.atlassian\.net/rest/api/3/issue/ACME-1/comment/.*")
        with pytest.raises(ToolError) as exc_info:
            await _client(site, http_client).delete_comment("ACME-1", "10050\n")
        assert not route.called
    assert "[site=acme] jira_delete_comment:" in str(exc_info.value)
    assert "comment_id" in str(exc_info.value)


async def test_delete_comment_invalid_url_is_shaped_as_a_tool_error() -> None:
    """A real ``httpx.InvalidURL`` can't occur once ``issue_key``/``comment_id``
    pass this client's own ``_path_segment`` checks -- this proves the
    transport-error handling still SHAPES one if it somehow did, rather than
    letting it escape unshaped (``httpx.InvalidURL`` is NOT an
    ``httpx.HTTPError`` subclass, so a plain ``except httpx.HTTPError``
    doesn't catch it -- the exact gap this closes)."""

    class _RaisingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.InvalidURL("bad url")

    site = _cloud_site()
    async with httpx.AsyncClient(transport=_RaisingTransport()) as http:
        client = _client(site, http)
        with pytest.raises(ToolError) as exc_info:
            await client.delete_comment("ACME-1", "10050")

    assert str(exc_info.value).startswith("[site=acme] jira_delete_comment:")
