"""Drives the mirrored parent server (built from two in-process fake
children) through a real Client: proves `site` stripping, cross-site
routing, error/timeout shaping, and that a wrapper-owned tool is shadowed."""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import mcp_types
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import ClientTransport, FastMCPTransport
from fastmcp.exceptions import ToolError

from mcp_atlassian_better.attachments import AttachmentClientRegistry
from mcp_atlassian_better.children import ChildManager
from mcp_atlassian_better.mirror import LateMirror, MultiSiteProxyTool, build_mirrored_tools
from mcp_atlassian_better.model import Defaults, SiteConfig, UpstreamConfig
from mcp_atlassian_better.registry import SiteRegistry
from mcp_atlassian_better.secrets import Secret
from mcp_atlassian_better.tools_meta import CURATED_TOOLS, WRAPPER_OWNED_TOOLS
from mcp_atlassian_better.wrapper_tools import build_attachment_tools, build_jira_sites_tool
from tests.fakes.fake_child import make_fake_child

_ALLOWLIST = CURATED_TOOLS | {
    "jira_boom",
    "jira_slow",
    "jira_write_blocked",
    "jira_destructive_only",
    "jira_get_issue_or_404",
}
_TIMEOUT = 1.0

_Rig = tuple[Client[ClientTransport], ChildManager, SiteRegistry]


def _text(result: mcp_types.CallToolResult) -> str:
    """First content block's text, asserting it's actually a TextContent (every
    error path in mirror.py only ever emits TextContent)."""
    block = result.content[0]
    assert isinstance(block, mcp_types.TextContent)
    return block.text


def _site(name: str, *prefixes: str, read_only: bool = False) -> SiteConfig:
    return SiteConfig(
        name=name,
        url=f"https://{name}.atlassian.net",
        key_prefixes=prefixes,
        username="you@example.com",
        api_token=Secret("token"),
        read_only=read_only,
    )


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncGenerator[_Rig, None]:
    registry = SiteRegistry([_site("acme", "ACME"), _site("beta", "BETA", read_only=True)])
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name)),
    )

    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        tools = await manager.discover_tools()
        mirrored = build_mirrored_tools(tools, manager, registry, _ALLOWLIST, timeout=_TIMEOUT)

        parent = FastMCP("test-parent")
        parent.add_tool(build_jira_sites_tool(manager, Defaults()))
        for tool in mirrored:
            parent.add_tool(tool)

        async with Client(FastMCPTransport(parent)) as client:
            yield client, manager, registry


async def test_every_mirrored_tool_has_site_in_its_schema(rig: _Rig) -> None:
    client, _, _ = rig
    tools = await client.list_tools()
    mirrored = [t for t in tools if t.name != "jira_sites"]
    assert mirrored
    for tool in mirrored:
        assert "site" in tool.input_schema["properties"], tool.name


async def test_download_attachments_is_shadowed_from_the_parent(rig: _Rig) -> None:
    client, _, _ = rig
    tools = await client.list_tools()
    names = {t.name for t in tools}
    assert "jira_download_attachments" not in names
    assert "jira_sites" in names  # the wrapper's own tool, not mirrored from a child
    # No *mirrored* (child-shadowing) wrapper-owned tool should ever leak through.
    assert (WRAPPER_OWNED_TOOLS - {"jira_sites"}).isdisjoint(names)


async def test_upstream_download_is_shadowed(tmp_path: Path) -> None:
    """The fake child advertises its own (base64) ``jira_download_attachments``;
    the parent must expose exactly one tool by that name -- ours, distinguished
    by its ``target_dir`` parameter that the upstream tool's schema never had."""
    registry = SiteRegistry([_site("acme", "ACME")])
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name)),
    )
    attachment_clients = AttachmentClientRegistry(registry.sites, Defaults(), redact=manager.redact)

    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        tools = await manager.discover_tools()
        mirrored = build_mirrored_tools(tools, manager, registry, _ALLOWLIST, timeout=_TIMEOUT)

        parent = FastMCP("test-parent")
        parent.add_tool(build_jira_sites_tool(manager, Defaults()))
        for attachment_tool in build_attachment_tools(registry, attachment_clients):
            parent.add_tool(attachment_tool)
        for tool in mirrored:
            parent.add_tool(tool)

        async with Client(FastMCPTransport(parent)) as client:
            listed = await client.list_tools()
            download_tools = [t for t in listed if t.name == "jira_download_attachments"]
            assert len(download_tools) == 1
            assert "target_dir" in download_tools[0].input_schema["properties"]
            names = {t.name for t in listed}
            assert "jira_list_attachments" in names
            assert "jira_upload_attachments" in names


async def test_site_is_stripped_before_forwarding_to_the_child(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_get_issue", {"issue_key": "ACME-1", "site": "acme"})
    assert result.is_error is False
    assert result.structured_content == {"site": "acme", "issue_key": "ACME-1"}


async def test_issue_key_prefix_routes_to_the_matching_child(rig: _Rig) -> None:
    client, _, _ = rig
    acme = await client.call_tool_mcp("jira_get_issue", {"issue_key": "ACME-1"})
    beta = await client.call_tool_mcp("jira_get_issue", {"issue_key": "BETA-1"})
    assert acme.structured_content == {"site": "acme", "issue_key": "ACME-1"}
    assert beta.structured_content == {"site": "beta", "issue_key": "BETA-1"}


async def test_explicit_site_wins_over_inference(rig: _Rig) -> None:
    client, _, _ = rig
    # ACME-1 would normally infer 'acme'; force 'beta' instead.
    result = await client.call_tool_mcp("jira_get_issue", {"issue_key": "ACME-1", "site": "beta"})
    assert result.structured_content == {"site": "beta", "issue_key": "ACME-1"}


async def test_ambiguous_call_is_a_tool_error_naming_every_prefix(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_search", {"jql": "project = FOO"})
    assert result.is_error is True
    text = _text(result)
    assert "acme: ACME" in text
    assert "beta: BETA" in text
    assert "jql" in text


async def test_unknown_prefix_names_it_in_the_error(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_get_issue", {"issue_key": "ZZZ-1"})
    assert result.is_error is True
    assert "ZZZ" in _text(result)


async def test_cross_site_arguments_raise_an_error(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp(
        "jira_create_issue_link",
        {
            "link_type": "Blocks",
            "inward_issue_key": "ACME-1",
            "outward_issue_key": "BETA-1",
        },
    )
    assert result.is_error is True
    assert "acme" in _text(result)
    assert "beta" in _text(result)


async def test_enabled_tools_restriction_is_enforced_by_the_wrapper_even_if_the_child_serves_the_tool(
    tmp_path: Path,
) -> None:
    """Belt: if a child ever serves a tool outside its own configured
    ENABLED_TOOLS (e.g. the upstream empty-string 'no filter' bug this same
    round fixed, or any future child misbehavior), the wrapper itself must
    still refuse a call the site's `enabled_tools` doesn't cover. The fake
    child here doesn't simulate ENABLED_TOOLS filtering at all -- standing in
    for exactly that misbehavior -- so this proves the refusal comes from
    the wrapper's own `enforce_site_policy` call, not from the child."""
    registry = SiteRegistry(
        [
            SiteConfig(
                name="acme",
                url="https://acme.atlassian.net",
                key_prefixes=("ACME",),
                username="you@example.com",
                api_token=Secret("token"),
                enabled_tools=frozenset({"jira_get_issue"}),
            )
        ]
    )
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name)),
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        tools = await manager.discover_tools()
        mirrored = build_mirrored_tools(tools, manager, registry, _ALLOWLIST, timeout=_TIMEOUT)
        parent = FastMCP("test-parent")
        for tool in mirrored:
            parent.add_tool(tool)
        async with Client(FastMCPTransport(parent)) as client:
            result = await client.call_tool_mcp("jira_boom", {"site": "acme"})

    assert result.is_error is True
    text = _text(result)
    assert "enabled_tools" in text


async def test_child_is_error_gets_a_site_prefix(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_boom", {"site": "acme"})
    assert result.is_error is True
    assert _text(result) == "[site=acme] boom"


async def test_read_only_hint_is_appended_for_a_blocked_write(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_write_blocked", {"issue_key": "BETA-1", "site": "beta"})
    assert result.is_error is True
    assert "[site=beta]" in _text(result)
    assert "read_only = true" in _text(result)


async def test_read_only_hint_is_absent_for_a_non_read_only_site(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_write_blocked", {"issue_key": "ACME-1", "site": "acme"})
    assert result.is_error is True
    assert "read_only" not in _text(result)


async def test_read_only_hint_is_appended_for_a_write_tool_with_no_annotations(rig: _Rig) -> None:
    """The real-world case: 23 of 25 upstream write tools (mcp-atlassian
    0.23.1) set no annotations at all, so `annotations` is None, not a
    readOnlyHint of False -- the hint must still fire for these."""
    client, _, _ = rig
    result = await client.call_tool_mcp(
        "jira_add_comment", {"issue_key": "BETA-1", "body": "hi", "site": "beta"}
    )
    assert result.is_error is True
    assert "[site=beta]" in _text(result)
    assert "read_only = true" in _text(result)


async def test_read_only_hint_is_appended_for_a_destructive_only_tool(rig: _Rig) -> None:
    """A tool that sets only destructiveHint leaves read_only_hint at its
    default None (never False) -- must not be mistaken for "known read-only"."""
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_destructive_only", {"issue_key": "BETA-1", "site": "beta"})
    assert result.is_error is True
    assert "read_only = true" in _text(result)


async def test_read_only_hint_is_absent_for_a_declared_read_only_tool(rig: _Rig) -> None:
    """A tool the child explicitly marked readOnlyHint=True (e.g. real
    jira_get_issue) failing for an unrelated reason (not-found) must not get
    the read-only-site hint tacked on."""
    client, _, _ = rig
    result = await client.call_tool_mcp(
        "jira_get_issue_or_404", {"issue_key": "BETA-99999999", "site": "beta"}
    )
    assert result.is_error is True
    assert "does not exist" in _text(result)
    assert "read_only" not in _text(result)


async def test_child_is_error_text_is_redacted(tmp_path: Path) -> None:
    """A child's `isError` body reaching the model unredacted (as opposed to
    the exception path in the `except` branch above) would leak a credential
    the child happened to echo back -- e.g. in a malformed-auth error body."""
    secret = "zz-unique-secret-zz"
    registry = SiteRegistry(
        [
            SiteConfig(
                name="acme",
                url="https://acme.atlassian.net",
                key_prefixes=("ACME",),
                username="you@example.com",
                api_token=Secret(secret),
            )
        ]
    )
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name, leak_secret=secret)),
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        tools = await manager.discover_tools()
        mirrored = build_mirrored_tools(
            tools, manager, registry, _ALLOWLIST | {"jira_leaky"}, timeout=_TIMEOUT
        )
        parent = FastMCP("test-parent")
        for tool in mirrored:
            parent.add_tool(tool)
        async with Client(FastMCPTransport(parent)) as client:
            result = await client.call_tool_mcp("jira_leaky", {"site": "acme"})
            assert result.is_error is True
            text = _text(result)
            assert secret not in text
            assert "***" in text


async def test_timeout_message_names_site_tool_and_log_path(rig: _Rig) -> None:
    client, manager, _ = rig
    result = await client.call_tool_mcp("jira_slow", {"site": "acme"})
    assert result.is_error is True
    text = _text(result)
    assert "[site=acme]" in text
    assert "jira_slow" in text
    assert "timed out after" in text
    assert str(manager.log_path("acme")) in text


async def test_success_meta_carries_site_and_reason(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_get_issue", {"issue_key": "ACME-1"})
    assert result.meta is not None
    assert result.meta["mcp-atlassian-better/site"] == "acme"
    assert "ACME-1" in result.meta["mcp-atlassian-better/site_reason"]


async def test_jira_sites_shape_and_no_credentials(rig: _Rig) -> None:
    client, _, _ = rig
    result = await client.call_tool_mcp("jira_sites", {})
    assert result.is_error is False
    payload = result.structured_content
    assert "note" not in payload  # both configured sites are healthy
    sites = payload["sites"]
    assert {s["name"] for s in sites} == {"acme", "beta"}
    for site in sites:
        assert site["state"] == "healthy"
        assert "token" not in str(site).lower()
        assert "api_token" not in site
        assert "password" not in str(site).lower()
    acme = next(s for s in sites if s["name"] == "acme")
    beta = next(s for s in sites if s["name"] == "beta")
    assert acme["discovery_source"] is True
    assert beta["discovery_source"] is False
    assert acme["host"] == "acme.atlassian.net"
    assert acme["key_prefixes"] == ["ACME"]
    assert acme["enabled_tools_restricted"] is False
    assert acme["recovery_attempts"] == 0
    assert acme["next_retry_at"] is None


async def test_jira_sites_note_when_every_healthy_site_is_read_only(tmp_path: Path) -> None:
    """Distinct from the no-healthy-site case below: every child IS
    reachable, but none is allowed to serve a write tool."""
    registry = SiteRegistry([_site("acme", "ACME", read_only=True), _site("beta", "BETA", read_only=True)])
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name)),
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        parent = FastMCP("test-parent")
        parent.add_tool(build_jira_sites_tool(manager, Defaults()))
        async with Client(FastMCPTransport(parent)) as client:
            result = await client.call_tool_mcp("jira_sites", {})
            payload = result.structured_content
            assert all(s["state"] == "healthy" for s in payload["sites"])
            assert "note" in payload
            assert "read_only" in payload["note"]


async def test_jira_sites_note_when_no_site_is_healthy(tmp_path: Path) -> None:
    registry = SiteRegistry([_site("acme", "ACME")])

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        raise RuntimeError("simulated connect failure")

    manager = ChildManager(registry, UpstreamConfig(), Defaults(), tmp_path, transport_factory=factory)
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        tools = await manager.discover_tools()
        parent = FastMCP("test-parent")
        parent.add_tool(build_jira_sites_tool(manager, Defaults()))
        async with Client(FastMCPTransport(parent)) as client:
            result = await client.call_tool_mcp("jira_sites", {})
            payload = result.structured_content
            assert payload["sites"][0]["state"] == "failed"
            assert "note" in payload
            assert "no configured site is currently healthy" in payload["note"]
    assert tools == []


async def test_jira_sites_mirrors_real_tools_after_every_site_recovers_from_startup_failure(
    tmp_path: Path,
) -> None:
    """The coordinator-added MEDIUM finding: if EVERY site fails at startup,
    `discover_tools()` returns nothing and zero tools are ever mirrored --
    and since a mirrored tool call is the only other thing that ever drives
    a failed site's recovery (`ChildManager.client_for`), nothing would ever
    call it again either. `jira_sites` must both recover sites itself
    (`recover_failed_sites`) and, the first time one succeeds, register real
    mirrored tools onto the already-running server (`LateMirror`)."""
    registry = SiteRegistry([_site("acme", "ACME")])
    attempts = {"count": 0}

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("simulated connect failure")  # the startup attempt
        return FastMCPTransport(make_fake_child(site.name))  # every later (recovery) attempt

    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(recovery_cooldown_seconds=0.05),
        tmp_path,
        transport_factory=factory,
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        tools = await manager.discover_tools()
        mirrored = build_mirrored_tools(tools, manager, registry, _ALLOWLIST, timeout=_TIMEOUT)
        assert mirrored == []  # the every-site-failed starting condition

        parent = FastMCP("test-parent")
        late_mirror = LateMirror(
            parent, manager, registry, _ALLOWLIST, _TIMEOUT, already_mirrored=bool(mirrored)
        )
        parent.add_tool(build_jira_sites_tool(manager, Defaults(), late_mirror=late_mirror))

        async with Client(FastMCPTransport(parent)) as client:
            names_before = {t.name for t in await client.list_tools()}
            assert names_before == {"jira_sites"}

            await anyio.sleep(0.1)  # past the cooldown
            result = await client.call_tool_mcp("jira_sites", {})
            assert not result.is_error
            assert attempts["count"] == 2  # exactly one recovery attempt

            # The real Client used here DOES support tools/list_changed
            # (fastmcp negotiates that capability for it), so LateMirror's
            # notification must have actually succeeded -- no "could not be
            # notified" fallback note, proving `send_tool_list_changed`
            # itself worked rather than silently failing every time.
            payload = result.structured_content
            assert "note" not in (payload or {})

            names_after = {t.name for t in await client.list_tools()}
            assert names_after > names_before  # real tools now mirrored, not just jira_sites
            assert "jira_get_issue" in names_after

            # A second call must be a no-op: no new factory call (already
            # healthy) and no duplicate re-add at all (`_mirrored` short-
            # circuits before `discover_tools()` runs again -- not because a
            # duplicate `add_tool` would crash; fastmcp 4.0.3's default
            # on_duplicate="warn" replaces it instead).
            result_again = await client.call_tool_mcp("jira_sites", {})
            assert not result_again.is_error
            assert attempts["count"] == 2
            assert {t.name for t in await client.list_tools()} == names_after


class _GatedRecoveryTransport(ClientTransport):
    """Blocks ``connect_session`` until ``release`` is set, then hands off to
    a real in-process fake child -- lets a test hold a recovery attempt open
    past ``jira_sites``'s own health-recovery budget, then let it actually
    succeed, without a real sleep-based race."""

    def __init__(self, site_name: str, release: anyio.Event) -> None:
        self._site_name = site_name
        self._release = release

    @contextlib.asynccontextmanager
    async def connect_session(  # type: ignore[override]
        self, *, transport_options: object = None, **session_kwargs: object
    ) -> AsyncIterator[object]:
        await self._release.wait()
        async with FastMCPTransport(make_fake_child(self._site_name)).connect_session(
            transport_options=transport_options,  # type: ignore[arg-type]
            **session_kwargs,  # type: ignore[arg-type]
        ) as session:
            yield session


async def test_jira_sites_reports_recovering_when_the_health_recovery_budget_expires(
    tmp_path: Path,
) -> None:
    """The M4a MEDIUM 1 finding: `jira_sites` used to await
    `recover_failed_sites()` with no bound of its own, so a site simply slow
    to connect (well within its own generous `connect_timeout_seconds`)
    could make a single `jira_sites` call hang for just as long. The
    recovery attempt itself must keep running in the background past the
    budget -- in a task group `ChildManager` owns, not the caller's -- so a
    later call still observes it finish."""
    registry = SiteRegistry([_site("acme", "ACME")])
    release = anyio.Event()
    attempts = {"count": 0}

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("simulated connect failure")  # the startup attempt
        return _GatedRecoveryTransport(site.name, release)  # every recovery attempt

    defaults = Defaults(
        recovery_cooldown_seconds=0.05,
        health_recovery_budget_seconds=0.3,
        connect_timeout_seconds=30.0,
    )
    manager = ChildManager(registry, UpstreamConfig(), defaults, tmp_path, transport_factory=factory)

    async with AsyncExitStack() as stack, manager.recovery_supervisor():
        await manager.start_all(stack, connect_timeout=5)
        parent = FastMCP("test-parent")
        parent.add_tool(build_jira_sites_tool(manager, defaults))

        async with Client(FastMCPTransport(parent)) as client:
            await anyio.sleep(0.1)  # past the cooldown

            before = time.monotonic()
            with anyio.fail_after(5.0):
                result = await client.call_tool_mcp("jira_sites", {})
            elapsed = time.monotonic() - before

            assert elapsed < 1.0  # bounded by the recovery budget, not the 30s connect timeout
            payload = result.structured_content
            assert payload["sites"][0]["state"] == "recovering"
            assert "note" in payload["sites"][0]
            assert attempts["count"] == 2  # the background attempt is genuinely in flight

            release.set()
            await anyio.sleep(0.2)  # let the background recovery attempt actually finish

            result_again = await client.call_tool_mcp("jira_sites", {})
            payload_again = result_again.structured_content
            assert payload_again["sites"][0]["state"] == "healthy"

        await manager.aclose()


async def test_discover_tools_prefers_unrestricted_site_over_read_only(
    tmp_path: Path,
) -> None:
    registry = SiteRegistry([_site("acme", "ACME", read_only=True), _site("beta", "BETA")])
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name)),
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        await manager.discover_tools()
        health = {h["name"]: h for h in manager.health()}
        assert health["beta"]["discovery_source"] is True
        assert health["acme"]["discovery_source"] is False


class _GatedDiscoveryManager:
    """A fake `ChildManager` whose `discover_tools()` starts, signals
    `entered`, then hangs forever -- lets a test deterministically catch
    `LateMirror` mid-discovery before racing a second caller against its
    try-lock, without a real ChildManager/transport at all."""

    def __init__(self, entered: anyio.Event) -> None:
        self._entered = entered
        self.calls = 0

    async def discover_tools(self) -> list[mcp_types.Tool]:
        self.calls += 1
        self._entered.set()
        await anyio.sleep_forever()
        return []  # pragma: no cover - unreachable


class _NoopSession:
    async def send_tool_list_changed(self) -> None:
        pass


class _FakeLateMirrorCtx:
    session = _NoopSession()


async def test_after_recovery_bounds_discover_tools_and_a_concurrent_caller_gets_the_fast_path() -> None:
    """The M4a MEDIUM 2 finding: `discover_tools()` used to run unbounded
    while `LateMirror` held its lock (one slow/stuck child's `list_tools()`
    could stall every `jira_sites` call forever), and a second concurrent
    caller used to queue behind a plain `anyio.Lock` instead of failing
    fast."""
    registry = SiteRegistry([_site("acme", "ACME")])
    entered = anyio.Event()
    manager = _GatedDiscoveryManager(entered)
    parent = FastMCP("test-parent")
    late_mirror = LateMirror(parent, manager, registry, _ALLOWLIST, timeout=0.3)  # type: ignore[arg-type]

    results: dict[str, object] = {}

    async def _first() -> None:
        results["first"] = await late_mirror.after_recovery(_FakeLateMirrorCtx())  # type: ignore[arg-type]

    async def _second() -> None:
        await entered.wait()  # the first caller is now genuinely inside discover_tools()
        results["second"] = await late_mirror.after_recovery(_FakeLateMirrorCtx())  # type: ignore[arg-type]

    before = time.monotonic()
    with anyio.fail_after(5.0):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_first)
            tg.start_soon(_second)
    elapsed = time.monotonic() - before

    assert elapsed < 2.0  # `_first` was bounded by `timeout`, not left hanging forever
    assert results["first"] is None  # timed out -> left unmirrored for a retry
    assert results["second"] is None  # the try-lock fast path, not queued behind `_first`
    assert manager.calls == 1  # `_second` never called discover_tools() itself
    assert late_mirror._mirrored is False  # noqa: SLF001 - whitebox


class _HangingDiscoveryManager:
    """A fake manager whose ``discover_tools()`` hangs forever (standing in
    for a SIGSTOP'd healthy child whose ``list_tools()`` never returns), but
    whose other ``jira_sites``-facing methods behave like an all-healthy,
    nothing-to-recover server -- isolates the M4a round-4 MEDIUM finding from
    needing a real child process at all."""

    def __init__(self) -> None:
        self.discover_calls = 0

    async def recover_failed_sites(self) -> list[str]:
        return []

    def health(self) -> list[dict[str, object]]:
        return [{"name": "acme", "state": "healthy", "read_only": False, "enabled_tools_restricted": False}]

    def upstream_version(self) -> str | None:
        return None

    async def discover_tools(self) -> list[mcp_types.Tool]:
        self.discover_calls += 1
        await anyio.sleep_forever()
        return []  # pragma: no cover - unreachable


async def test_jira_sites_is_bounded_by_the_health_recovery_budget_even_when_late_mirror_is_stuck() -> None:
    """The M4a round-4 MEDIUM finding: `LateMirror.after_recovery` used to
    run OUTSIDE `jira_sites`'s own `anyio.move_on_after(health_recovery_budget_seconds)`
    scope, bounded only by the (much longer) `call_timeout_seconds` -- the
    real repro measured 120s against a budget of 2. A hanging
    `discover_tools()` (a SIGSTOP'd healthy child's `list_tools()` never
    returning) must not make a `jira_sites` call take any longer than the
    budget."""
    manager = _HangingDiscoveryManager()
    parent = FastMCP("test-parent")
    registry = SiteRegistry([_site("acme", "ACME")])
    late_mirror = LateMirror(parent, manager, registry, _ALLOWLIST, timeout=120.0)  # type: ignore[arg-type]
    defaults = Defaults(health_recovery_budget_seconds=1.0, call_timeout_seconds=120.0)
    parent.add_tool(build_jira_sites_tool(manager, defaults, late_mirror=late_mirror))  # type: ignore[arg-type]

    async with Client(FastMCPTransport(parent)) as client:
        before = time.monotonic()
        with anyio.fail_after(5.0):
            result = await client.call_tool_mcp("jira_sites", {})
        elapsed = time.monotonic() - before

    assert elapsed < 1.5, f"jira_sites took {elapsed:.2f}s against a 1.0s budget"
    assert manager.discover_calls == 1
    payload = result.structured_content
    assert "note" in payload
    assert "pending" in payload["note"]


class _DeadChildClient:
    """Stands in for a connection that broke mid-call (e.g. the child process
    died) -- distinct from a timeout, which anyio.fail_after handles."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def call_tool_mcp(self, name: str, arguments: dict[str, object]) -> mcp_types.CallToolResult:
        raise self._exc


class _DeadChildManager:
    def __init__(self, log_path: Path, exc: Exception | None = None, secret: str = "") -> None:
        self._log_path = log_path
        self._exc = exc or ConnectionError("child pipe broke")
        self._secret = secret
        self.marked_failed: list[str] = []

    async def client_for(self, site_name: str) -> _DeadChildClient:
        return _DeadChildClient(self._exc)

    def generation(self, site_name: str) -> int:
        return 0

    def log_path(self, site_name: str) -> Path:
        return self._log_path

    def redact(self, text: str) -> str:
        return text.replace(self._secret, "***") if self._secret else text

    def mark_failed(self, site_name: str, reason: str, *, generation: int | None = None) -> None:
        self.marked_failed.append(site_name)

    def mark_timeout(self, site_name: str, reason: str, *, generation: int | None = None) -> None:
        pass

    def mark_success(self, site_name: str) -> None:
        pass


async def test_non_timeout_child_failure_is_shaped_like_a_site_error(tmp_path: Path) -> None:
    registry = SiteRegistry([_site("acme", "ACME")])
    log_path = tmp_path / "acme.log"
    manager = _DeadChildManager(log_path)
    mcp_tool = mcp_types.Tool(
        name="jira_get_issue",
        input_schema={"type": "object", "properties": {"issue_key": {"type": "string"}}},
    )
    tool = MultiSiteProxyTool.from_mcp_tool(manager, registry, mcp_tool, timeout=5.0)  # type: ignore[arg-type]

    with pytest.raises(ToolError) as exc_info:
        await tool.run({"issue_key": "ACME-1"})

    message = str(exc_info.value)
    assert "[site=acme]" in message
    assert "ConnectionError" in message
    assert "child pipe broke" in message
    assert str(log_path) in message


async def test_non_timeout_child_failure_redacts_a_leaked_secret(tmp_path: Path) -> None:
    registry = SiteRegistry([_site("acme", "ACME")])
    log_path = tmp_path / "acme.log"
    manager = _DeadChildManager(
        log_path,
        exc=ConnectionError("child pipe broke near token super-secret-token"),
        secret="super-secret-token",
    )
    mcp_tool = mcp_types.Tool(
        name="jira_get_issue",
        input_schema={"type": "object", "properties": {"issue_key": {"type": "string"}}},
    )
    tool = MultiSiteProxyTool.from_mcp_tool(manager, registry, mcp_tool, timeout=5.0)  # type: ignore[arg-type]

    with pytest.raises(ToolError) as exc_info:
        await tool.run({"issue_key": "ACME-1"})

    message = str(exc_info.value)
    assert "super-secret-token" not in message
    assert "***" in message


async def test_connection_level_failure_marks_the_site_failed(tmp_path: Path) -> None:
    registry = SiteRegistry([_site("acme", "ACME")])
    manager = _DeadChildManager(tmp_path / "acme.log", exc=anyio.ClosedResourceError())
    mcp_tool = mcp_types.Tool(
        name="jira_get_issue",
        input_schema={"type": "object", "properties": {"issue_key": {"type": "string"}}},
    )
    tool = MultiSiteProxyTool.from_mcp_tool(manager, registry, mcp_tool, timeout=5.0)  # type: ignore[arg-type]

    with pytest.raises(ToolError):
        await tool.run({"issue_key": "ACME-1"})

    assert manager.marked_failed == ["acme"]


class _HangingClient:
    async def call_tool_mcp(self, name: str, arguments: dict[str, object]) -> mcp_types.CallToolResult:
        await anyio.sleep(3600)
        raise AssertionError("unreachable")


class _HangingChildManagerSpy:
    """Stands in for a manager whose child never answers -- exercises the
    `except TimeoutError` branch in isolation, independent of `_DeadChildManager`
    (which models a BROKEN connection, a different failure shape)."""

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self.timeouts: list[str] = []

    async def client_for(self, site_name: str) -> _HangingClient:
        return _HangingClient()

    def generation(self, site_name: str) -> int:
        return 0

    def log_path(self, site_name: str) -> Path:
        return self._log_path

    def redact(self, text: str) -> str:
        return text

    def mark_failed(self, site_name: str, reason: str, *, generation: int | None = None) -> None:
        pass

    def mark_timeout(self, site_name: str, reason: str, *, generation: int | None = None) -> None:
        self.timeouts.append(site_name)

    def mark_success(self, site_name: str) -> None:
        pass


async def test_call_timeout_records_a_mark_timeout_call(tmp_path: Path) -> None:
    registry = SiteRegistry([_site("acme", "ACME")])
    manager = _HangingChildManagerSpy(tmp_path / "acme.log")
    mcp_tool = mcp_types.Tool(
        name="jira_get_issue",
        input_schema={"type": "object", "properties": {"issue_key": {"type": "string"}}},
    )
    tool = MultiSiteProxyTool.from_mcp_tool(manager, registry, mcp_tool, timeout=0.01)  # type: ignore[arg-type]

    with pytest.raises(ToolError, match="timed out"):
        await tool.run({"issue_key": "ACME-1"})

    assert manager.timeouts == ["acme"]


class _SlowClientForManagerSpy:
    """``client_for`` itself is slow (models the MEDIUM fix: recovery now
    runs inside ``client_for``, which must be bounded by the caller's own
    call timeout) -- distinct from ``_HangingChildManagerSpy`` above, where
    ``client_for`` resolves instantly but the call itself hangs."""

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self.timeouts: list[str] = []

    async def client_for(self, site_name: str) -> _HangingClient:
        await anyio.sleep(3600)
        raise AssertionError("unreachable")

    def generation(self, site_name: str) -> int:
        return 0

    def log_path(self, site_name: str) -> Path:
        return self._log_path

    def redact(self, text: str) -> str:
        return text

    def mark_failed(self, site_name: str, reason: str, *, generation: int | None = None) -> None:
        pass

    def mark_timeout(self, site_name: str, reason: str, *, generation: int | None = None) -> None:
        self.timeouts.append(site_name)

    def mark_success(self, site_name: str) -> None:
        pass


async def test_call_timeout_bounds_a_slow_client_for_not_just_the_call(tmp_path: Path) -> None:
    """The MEDIUM finding: ``client_for`` (and any recovery it triggers) now
    runs INSIDE ``anyio.fail_after(self.timeout)``, not before it -- proves a
    slow/hung ``client_for`` is bounded by the tool's own call timeout rather
    than running for free ahead of it. ``mark_timeout`` is NOT called here:
    ``client_for`` never returned a client (no call was ever actually made),
    so there's nothing for `mark_timeout` to attribute to a real call --
    the real `ChildManager`'s own recovery-cancellation handling (see
    `children.py`) already records the failure with a more specific reason."""
    registry = SiteRegistry([_site("acme", "ACME")])
    manager = _SlowClientForManagerSpy(tmp_path / "acme.log")
    mcp_tool = mcp_types.Tool(
        name="jira_get_issue",
        input_schema={"type": "object", "properties": {"issue_key": {"type": "string"}}},
    )
    tool = MultiSiteProxyTool.from_mcp_tool(manager, registry, mcp_tool, timeout=0.01)  # type: ignore[arg-type]

    with pytest.raises(ToolError, match="timed out"):
        await tool.run({"issue_key": "ACME-1"})

    assert manager.timeouts == []


class _RecoveringChildManagerSpy:
    """``client_for`` raises its own already-shaped ``ToolError`` directly --
    the real ``ChildManager.client_for`` does this for "site is unavailable"
    and "is recovering; retry shortly" -- must NOT be double-wrapped into
    "... failed: ToolError: ..."."""

    def __init__(self, log_path: Path, message: str) -> None:
        self._log_path = log_path
        self._message = message

    async def client_for(self, site_name: str) -> _HangingClient:
        raise ToolError(self._message)

    def generation(self, site_name: str) -> int:
        return 0

    def log_path(self, site_name: str) -> Path:
        return self._log_path

    def redact(self, text: str) -> str:
        return text

    def mark_failed(self, site_name: str, reason: str, *, generation: int | None = None) -> None:
        pass

    def mark_timeout(self, site_name: str, reason: str, *, generation: int | None = None) -> None:
        pass

    def mark_success(self, site_name: str) -> None:
        pass


async def test_a_tool_error_from_client_for_is_not_double_wrapped(tmp_path: Path) -> None:
    registry = SiteRegistry([_site("acme", "ACME")])
    manager = _RecoveringChildManagerSpy(tmp_path / "acme.log", "Site 'acme' is recovering; retry shortly")
    mcp_tool = mcp_types.Tool(
        name="jira_get_issue",
        input_schema={"type": "object", "properties": {"issue_key": {"type": "string"}}},
    )
    tool = MultiSiteProxyTool.from_mcp_tool(manager, registry, mcp_tool, timeout=5.0)  # type: ignore[arg-type]

    with pytest.raises(ToolError) as exc_info:
        await tool.run({"issue_key": "ACME-1"})

    message = str(exc_info.value)
    assert message == "Site 'acme' is recovering; retry shortly"
    assert "failed:" not in message


async def test_generic_application_failure_does_not_mark_the_site_failed(tmp_path: Path) -> None:
    registry = SiteRegistry([_site("acme", "ACME")])
    manager = _DeadChildManager(tmp_path / "acme.log", exc=RuntimeError("some other bug"))
    mcp_tool = mcp_types.Tool(
        name="jira_get_issue",
        input_schema={"type": "object", "properties": {"issue_key": {"type": "string"}}},
    )
    tool = MultiSiteProxyTool.from_mcp_tool(manager, registry, mcp_tool, timeout=5.0)  # type: ignore[arg-type]

    with pytest.raises(ToolError):
        await tool.run({"issue_key": "ACME-1"})

    assert manager.marked_failed == []
