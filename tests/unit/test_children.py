"""ChildManager: env construction, fail-soft startup, discovery source
selection, and health reporting -- all against in-process fake children."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import pytest
from fastmcp.client.transports import ClientTransport, FastMCPTransport, StdioTransport
from fastmcp.exceptions import ToolError

from mcp_atlassian_better.children import (
    BASE_ENV_PASSTHROUGH,
    EMPTY_ENABLED_TOOLS_SENTINEL,
    ChildManager,
    build_child_env,
    minimal_env,
)
from mcp_atlassian_better.model import Defaults, SiteConfig, UpstreamConfig
from mcp_atlassian_better.registry import SiteRegistry
from mcp_atlassian_better.secrets import Secret
from mcp_atlassian_better.tools_meta import CURATED_TOOLS, WRAPPER_OWNED_TOOLS
from tests.fakes.fake_child import make_fake_child


def _cloud_site(name: str, *prefixes: str, **overrides: object) -> SiteConfig:
    fields: dict[str, object] = {
        "name": name,
        "url": f"https://{name}.atlassian.net",
        "key_prefixes": prefixes,
        "username": "you@example.com",
        "api_token": Secret("token-value"),
    }
    fields.update(overrides)
    return SiteConfig(**fields)  # type: ignore[arg-type]


# --- build_child_env ---


def test_toolsets_is_always_all() -> None:
    site = _cloud_site("acme", "ACME")
    env = build_child_env(site, UpstreamConfig(), Defaults())
    assert env["TOOLSETS"] == "all"


def test_enabled_tools_is_curated_minus_wrapper_owned_by_default() -> None:
    site = _cloud_site("acme", "ACME")
    env = build_child_env(site, UpstreamConfig(), Defaults(toolset_preset="curated"))
    names = set(env["ENABLED_TOOLS"].split(","))
    assert names == CURATED_TOOLS - WRAPPER_OWNED_TOOLS
    assert "jira_download_attachments" not in names


def test_enabled_tools_excludes_all_wrapper_owned_attachment_names() -> None:
    site = _cloud_site("acme", "ACME")
    env = build_child_env(site, UpstreamConfig(), Defaults(toolset_preset="curated"))
    names = set(env["ENABLED_TOOLS"].split(","))
    assert names.isdisjoint(WRAPPER_OWNED_TOOLS)
    assert "jira_list_attachments" not in names
    assert "jira_upload_attachments" not in names
    assert "jira_download_attachments" not in names


def test_enabled_tools_omitted_for_all_preset_with_no_site_override() -> None:
    site = _cloud_site("acme", "ACME")
    env = build_child_env(site, UpstreamConfig(), Defaults(toolset_preset="all"))
    assert "ENABLED_TOOLS" not in env


def test_site_level_enabled_tools_override_wins_even_under_all_preset() -> None:
    site = _cloud_site(
        "acme", "ACME", enabled_tools=frozenset({"jira_get_issue", "jira_download_attachments"})
    )
    env = build_child_env(site, UpstreamConfig(), Defaults(toolset_preset="all"))
    assert env["ENABLED_TOOLS"] == "jira_get_issue"


def test_enabled_tools_of_only_wrapper_owned_names_forces_the_none_sentinel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A site whose enabled_tools is entirely WRAPPER_OWNED_TOOLS names (e.g.
    someone only wants the attachment tools on this site) would otherwise
    compute an empty ENABLED_TOOLS -- which upstream treats as "no filter",
    serving the child's FULL 63-tool set. The sentinel must be used instead."""
    site = _cloud_site(
        "acme", "ACME", enabled_tools=frozenset({"jira_download_attachments", "jira_list_attachments"})
    )
    with caplog.at_level("WARNING"):
        env = build_child_env(site, UpstreamConfig(), Defaults(toolset_preset="all"))
    assert env["ENABLED_TOOLS"] == EMPTY_ENABLED_TOOLS_SENTINEL
    assert any("enabled_tools" in record.message for record in caplog.records)


def test_read_only_mode_set_only_when_site_is_read_only() -> None:
    read_only_env = build_child_env(_cloud_site("acme", "ACME", read_only=True), UpstreamConfig(), Defaults())
    normal_env = build_child_env(_cloud_site("beta", "BETA"), UpstreamConfig(), Defaults())
    assert read_only_env["READ_ONLY_MODE"] == "true"
    assert "READ_ONLY_MODE" not in normal_env


def test_projects_filter_passed_through_when_configured() -> None:
    site = _cloud_site("acme", "ACME", projects_filter=("FOO", "BAR"))
    env = build_child_env(site, UpstreamConfig(), Defaults())
    assert env["JIRA_PROJECTS_FILTER"] == "FOO,BAR"


def test_cloud_auth_env_vars() -> None:
    site = _cloud_site("acme", "ACME")
    env = build_child_env(site, UpstreamConfig(), Defaults())
    assert env["JIRA_URL"] == "https://acme.atlassian.net"
    assert env["JIRA_USERNAME"] == "you@example.com"
    assert env["JIRA_API_TOKEN"] == "token-value"
    assert "JIRA_PERSONAL_TOKEN" not in env


def test_server_dc_auth_env_vars() -> None:
    site = SiteConfig(
        name="onprem",
        url="https://jira.example.com",
        key_prefixes=("ONPREM",),
        personal_token=Secret("pat-value"),
    )
    env = build_child_env(site, UpstreamConfig(), Defaults())
    assert env["JIRA_PERSONAL_TOKEN"] == "pat-value"
    assert "JIRA_API_TOKEN" not in env
    assert "JIRA_USERNAME" not in env


def test_minimal_env_passes_through_only_configured_and_base_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs.pem")
    monkeypatch.setenv("SOME_UNRELATED_SECRET", "should-not-appear")
    env = minimal_env(("SSL_CERT_FILE",))
    assert env["SSL_CERT_FILE"] == "/etc/ssl/certs.pem"
    assert "SOME_UNRELATED_SECRET" not in env
    assert set(BASE_ENV_PASSTHROUGH) <= {"PATH", "HOME"}


def test_build_child_env_never_leaks_unrelated_ambient_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_OTHER_APPS_SECRET", "leaked")
    site = _cloud_site("acme", "ACME")
    env = build_child_env(site, UpstreamConfig(), Defaults())
    assert "SOME_OTHER_APPS_SECRET" not in env


# --- ChildManager against fake children ---


def _make_manager(registry: SiteRegistry, tmp_path: Path, transport_factory: object) -> ChildManager:
    return ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(),
        tmp_path,
        transport_factory=transport_factory,  # type: ignore[arg-type]
    )


async def test_discovery_skips_a_read_only_first_site(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME", read_only=True), _cloud_site("beta", "BETA")])
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        await manager.discover_tools()
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["discovery_source"] is False
        assert health["beta"]["discovery_source"] is True


async def test_health_reports_each_sites_configured_source(tmp_path: Path) -> None:
    registry = SiteRegistry(
        [
            _cloud_site("acme", "ACME", source="sites.d/01-acme.toml"),
            _cloud_site("beta", "BETA", source="env"),
        ]
    )
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["source"] == "sites.d/01-acme.toml"
        assert health["beta"]["source"] == "env"


async def test_one_failing_site_leaves_the_other_healthy(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME"), _cloud_site("beta", "BETA")])

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        if site.name == "acme":
            raise RuntimeError("simulated connect failure")
        return FastMCPTransport(make_fake_child(site.name))

    manager = _make_manager(registry, tmp_path, factory)
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "failed"
        assert "simulated connect failure" in str(health["acme"]["error"])
        assert health["beta"]["state"] == "healthy"


async def test_call_to_failed_site_names_it_in_the_tool_error(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        raise RuntimeError("simulated connect failure")

    manager = _make_manager(registry, tmp_path, factory)
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        with pytest.raises(ToolError, match="acme"):
            await manager.client_for("acme")


async def test_all_sites_failed_means_no_tools_discovered(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME"), _cloud_site("beta", "BETA")])

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        raise RuntimeError("simulated connect failure")

    manager = _make_manager(registry, tmp_path, factory)
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        tools = await manager.discover_tools()
        assert tools == []
        assert all(h["state"] == "failed" for h in manager.health())


async def test_aclose_kills_a_real_child_process(tmp_path: Path) -> None:
    """Regression test for the M2r1 HIGH finding: with keep_alive=True,
    StdioTransport.connect_session's `finally` skips disconnect(), so merely
    unwinding an AsyncExitStack around the Client never asks the transport to
    kill the subprocess. ChildManager.aclose() must close every child's
    Client explicitly instead of relying on that unwind alone -- proved here
    against a REAL OS subprocess, not an in-process fake."""
    pid_file = tmp_path / "pid.txt"
    script = (
        "import os\n"
        f"with open({str(pid_file)!r}, 'w') as f:\n"
        "    f.write(str(os.getpid()))\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('probe')\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = _make_manager(
        registry,
        tmp_path,
        lambda site, up: StdioTransport(command=sys.executable, args=["-c", script], keep_alive=True),
    )

    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=15)
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "healthy"

        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # still alive; raises ProcessLookupError otherwise

        await manager.aclose()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await anyio.sleep(0.05)
        else:
            pytest.fail(f"child pid {pid} was still alive 5s after ChildManager.aclose()")


async def test_recovery_reuses_the_same_log_file_instead_of_leaking_one_fd_per_attempt(
    tmp_path: Path,
) -> None:
    """The MEDIUM finding: fastmcp's `StdioTransport` never closes a
    caller-supplied `log_file`, so opening a fresh one on every recovery
    attempt would leak one fd per attempt for the rest of the process's
    life. Proved against the REAL default transport factory (an actual,
    fast-exiting subprocess each cycle, not a fake) by counting THIS
    process's own open fds -- the log file is opened by the parent to hand
    to the child as its stderr, so a leak would show up here directly."""
    if not Path("/dev/fd").is_dir():
        pytest.skip("requires /dev/fd (not available on this platform)")

    script = (
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    sys.exit(0)\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('probe')\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = ChildManager(
        registry,
        UpstreamConfig(command=(sys.executable, "-c", script)),
        Defaults(recovery_cooldown_seconds=0.02, connect_timeout_seconds=10),
        tmp_path,
    )

    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=15)
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "healthy"

        baseline = len(os.listdir("/dev/fd"))

        for _ in range(5):
            manager.mark_failed("acme", "simulated pipe break")
            await anyio.sleep(0.05)  # past the cooldown
            await manager.client_for("acme")  # triggers one real recovery/respawn

        after = len(os.listdir("/dev/fd"))
        # A little fd noise (the listdir call itself, GC timing) is
        # tolerated; a real per-attempt leak would show as +5 (one per
        # recovery cycle), not +0 or +1.
        assert after - baseline <= 1, (
            f"open fd count grew by {after - baseline} across 5 recovery cycles -- a child log "
            "file was opened per attempt instead of being reused"
        )


async def test_cancelling_a_stuck_handshake_still_kills_the_spawned_child(tmp_path: Path) -> None:
    """Characterization test, not a regression test: this passes whether or
    not `handle.client` is assigned before or after `enter_async_context`,
    because fastmcp's own `Client._connect()` has a `CancelledError` handler
    that closes the transport on a cancelled connect regardless (verified
    empirically). It's still worth pinning explicitly -- `server.py`'s
    cancel-before-close shutdown ordering (see its module docstring) depends
    on this exact behavior to unblock a child stuck mid-handshake, and this
    proves it against a REAL subprocess that never speaks MCP, so the
    handshake hangs until cancelled."""
    pid_file = tmp_path / "pid.txt"
    script = (
        "import os, time\n"
        f"with open({str(pid_file)!r}, 'w') as f:\n"
        "    f.write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = _make_manager(
        registry,
        tmp_path,
        lambda site, up: StdioTransport(command=sys.executable, args=["-c", script], keep_alive=True),
    )

    async with AsyncExitStack() as stack:
        async with anyio.create_task_group() as tg:
            tg.start_soon(manager.start_all, stack, 30.0)
            deadline = time.monotonic() + 5.0
            while not pid_file.exists() and time.monotonic() < deadline:
                await anyio.sleep(0.02)
            assert pid_file.exists(), "child never started"
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # still alive

            # Simulates a shutdown signal landing mid-connect.
            tg.cancel_scope.cancel()

        await manager.aclose()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await anyio.sleep(0.05)
        else:
            pytest.fail(f"child pid {pid} was still alive 5s after cancel+aclose (orphaned)")


async def test_probe_failure_after_connect_keeps_the_client_for_later_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same HIGH finding: a child whose liveness probe
    fails AFTER its session was entered must still be closeable later --
    losing the reference here is exactly what let such a child leak for the
    rest of the process's life."""
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))

    async def _boom(client: object) -> None:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(manager, "_probe_liveness", _boom)

    closed: list[str] = []
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        handle = manager._handles["acme"]  # noqa: SLF001 - whitebox on our own fake
        assert handle.state == "failed"
        assert handle.client is not None

        original_close = handle.client.close

        async def _spy_close() -> None:
            closed.append("acme")
            await original_close()  # type: ignore[no-untyped-call]

        monkeypatch.setattr(handle.client, "close", _spy_close)
        await manager.aclose()

    assert closed == ["acme"]


async def test_connect_failure_error_is_redacted(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME", api_token=Secret("zz-unique-secret-zz"))])

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        raise RuntimeError("auth failed with token zz-unique-secret-zz")

    manager = _make_manager(registry, tmp_path, factory)
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        health = {h["name"]: h for h in manager.health()}
        assert "zz-unique-secret-zz" not in str(health["acme"]["error"])
        assert "***" in str(health["acme"]["error"])


class _HangingTransport(ClientTransport):
    """Never yields a session -- stands in for a real connect that hangs past
    the deadline, so ``_start_one``'s cancel scope is the thing that actually
    fires (``scope.cancelled_caught``), not merely a ``TimeoutError`` raised
    for some unrelated reason."""

    @contextlib.asynccontextmanager
    async def connect_session(  # type: ignore[override]
        self, *, transport_options: object = None, **session_kwargs: object
    ) -> AsyncIterator[object]:
        await anyio.sleep_forever()
        yield None  # pragma: no cover - unreachable, connect_session never yields


async def test_a_real_hang_past_the_deadline_reports_our_own_timeout_message(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = _make_manager(registry, tmp_path, lambda site, up: _HangingTransport())
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=0.2)
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["error"] == "connect timed out after 0.2s"


async def test_a_bare_timeouterror_raised_by_the_factory_is_not_relabeled_as_our_deadline(
    tmp_path: Path,
) -> None:
    """A plain ``TimeoutError`` an underlying library raises for its own
    reason (e.g. a real OS-level ETIMEDOUT) is not the same thing as OUR
    connect deadline expiring -- it must go through the generic
    "failed to start" path, not be mislabeled "connect timed out after Xs"."""
    registry = SiteRegistry([_cloud_site("acme", "ACME")])

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        raise TimeoutError

    manager = _make_manager(registry, tmp_path, factory)
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        health = {h["name"]: h for h in manager.health()}
        # Not "connect timed out after 5s" (our own deadline message) -- this
        # exception was never near the deadline; it just isn't the thing that
        # message means.
        assert health["acme"]["error"] == "TimeoutError: "


def test_redact_also_strips_a_cdn_urls_query_string(tmp_path: Path) -> None:
    """`redact` is used on the tool-result path (e.g. an attachment
    transport-error message) where a pre-signed CDN URL's query string can
    itself be a credential we never configured as a known `Secret` -- so it
    must be stripped unconditionally, not only known secret values."""
    registry = SiteRegistry([_cloud_site("acme", "ACME", api_token=Secret("zz-unique-secret-zz"))])
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))

    text = (
        "ConnectError: GET https://media-cdn.example-atlassian-media.net/path?token=SECRET failed "
        "near token zz-unique-secret-zz"
    )
    redacted = manager.redact(text)

    assert "token=SECRET" not in redacted
    assert "zz-unique-secret-zz" not in redacted
    assert "***" in redacted


async def test_mark_failed_redacts_the_reason_and_records_a_timestamp(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME", api_token=Secret("zz-unique-secret-zz"))])
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        manager.mark_failed("acme", "connection closed near token zz-unique-secret-zz")
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "failed"
        assert "zz-unique-secret-zz" not in str(health["acme"]["last_error"])
        assert "***" in str(health["acme"]["last_error"])
        assert health["acme"]["last_error_at"] is not None


async def test_mark_timeout_increments_and_flips_to_failed_after_three(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)

        manager.mark_timeout("acme", "jira_get_issue timed out after 1s")
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["timeouts"] == 1
        assert health["acme"]["state"] == "healthy"

        manager.mark_timeout("acme", "jira_get_issue timed out after 1s")
        manager.mark_timeout("acme", "jira_get_issue timed out after 1s")
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["timeouts"] == 3
        assert health["acme"]["state"] == "failed"


async def test_mark_timeout_does_not_re_arm_the_cooldown_past_the_transition_to_failed(
    tmp_path: Path,
) -> None:
    """The LOW finding: only the TRANSITION into `failed` may set
    `next_retry_at_monotonic` -- a slow trickle of further timeouts (from
    other still-in-flight calls) that each also cross the 3-consecutive
    threshold must not keep pushing the site's cooldown further out."""
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)

        manager.mark_timeout("acme", "timed out")
        manager.mark_timeout("acme", "timed out")
        manager.mark_timeout("acme", "timed out")  # the transition to failed
        handle = manager._handles["acme"]  # noqa: SLF001 - whitebox: health()'s epoch-seconds
        # estimate is recomputed against the CURRENT clock offset on every
        # read, so it drifts by a few microseconds between two calls even
        # when the underlying monotonic value hasn't changed -- read the raw
        # monotonic value directly for an exact equality check instead.
        assert handle.state == "failed"
        first_retry_at_monotonic = handle.next_retry_at_monotonic
        assert isinstance(first_retry_at_monotonic, float)

        manager.mark_timeout("acme", "a further stale timeout")  # already failed
        assert handle.state == "failed"
        assert handle.next_retry_at_monotonic == first_retry_at_monotonic  # not pushed further out


async def test_mark_success_resets_the_timeout_counter(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)

        manager.mark_timeout("acme", "timed out")
        manager.mark_timeout("acme", "timed out")
        manager.mark_success("acme")
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["timeouts"] == 0
        assert health["acme"]["state"] == "healthy"


async def test_mark_failed_with_a_stale_generation_is_a_no_op(tmp_path: Path) -> None:
    """A slow, still-in-flight call captured `generation` before a recovery
    already replaced the client it was using -- its eventual `mark_failed`
    must not clobber the NEWER client's healthy state (the LOW finding)."""
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(recovery_cooldown_seconds=0.05),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name)),
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        stale_generation = manager.generation("acme")
        assert stale_generation == 1

        manager.mark_failed("acme", "simulated pipe break")
        await anyio.sleep(0.1)  # past the (default) cooldown
        await manager.client_for("acme")  # recovers -> generation bumps to 2

        assert manager.generation("acme") == 2
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "healthy"

        # The stale call's verdict, arriving late, must not un-heal the site.
        manager.mark_failed("acme", "a stale call's late verdict", generation=stale_generation)

        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "healthy"


async def test_mark_timeout_with_a_stale_generation_is_a_no_op(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(recovery_cooldown_seconds=0.05),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name)),
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        stale_generation = manager.generation("acme")

        manager.mark_failed("acme", "simulated pipe break")
        await anyio.sleep(0.1)
        await manager.client_for("acme")  # recovers -> a newer generation

        manager.mark_timeout("acme", "a stale call's late timeout", generation=stale_generation)

        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["timeouts"] == 0
        assert health["acme"]["state"] == "healthy"


async def test_probe_upstream_version_captures_stdout(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    upstream = UpstreamConfig(command=(sys.executable, "-c", "import sys; print(sys.argv[-1])"))
    manager = ChildManager(registry, upstream, Defaults(), tmp_path)

    version = await manager.probe_upstream_version()

    assert version == "--version"
    assert manager.upstream_version() == "--version"


async def test_probe_upstream_version_stdin_is_devnull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upstream that doesn't recognize `--version` could start serving MCP
    on inherited stdin -- the live pipe this process itself uses to talk to
    Claude Code -- and eat its `initialize` request. A real-subprocess
    behavioral check is unreliable here (pytest's own default capturing
    already redirects fd 0 for any child, regardless of this code's own
    ``stdin=`` kwarg), so this asserts the kwarg directly; also proved end to
    end in the adversarial-loop real run."""
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = ChildManager(registry, UpstreamConfig(), Defaults(), tmp_path)
    captured_stdin: list[object] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_stdin.append(kwargs.get("stdin"))
        return subprocess.CompletedProcess(command, 0, stdout="1.0.0", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    version = await manager.probe_upstream_version(timeout=5)

    assert version == "1.0.0"
    assert captured_stdin == [subprocess.DEVNULL]


async def test_probe_upstream_version_handles_a_missing_command(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    upstream = UpstreamConfig(command=(str(tmp_path / "does-not-exist"),))
    manager = ChildManager(registry, upstream, Defaults(), tmp_path)

    version = await manager.probe_upstream_version()

    assert version is None
    assert manager.upstream_version() is None


async def test_probe_upstream_version_is_none_when_command_prints_nothing(tmp_path: Path) -> None:
    """Exit 0 with empty stdout is not a version -- `upstream_version()` must
    stay at its `None` default rather than reporting `""` as if it were one."""
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    upstream = UpstreamConfig(command=(sys.executable, "-c", "pass"))
    manager = ChildManager(registry, upstream, Defaults(), tmp_path)

    version = await manager.probe_upstream_version()

    assert version is None
    assert manager.upstream_version() is None


# --- failed-site recovery (M4) ---


async def test_client_for_recovers_a_failed_site_once_the_cooldown_elapses(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    attempts = {"count": 0}

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("simulated connect failure")
        return FastMCPTransport(make_fake_child(site.name))

    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(recovery_cooldown_seconds=0.05),
        tmp_path,
        transport_factory=factory,
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "failed"
        assert health["acme"]["next_retry_at"] is not None

        await anyio.sleep(0.1)  # past the cooldown
        client = await manager.client_for("acme")

        assert client is not None
        assert attempts["count"] == 2
        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "healthy"
        assert health["acme"]["recovery_attempts"] == 1
        assert health["acme"]["next_retry_at"] is None
        assert health["acme"]["error"] is None


async def test_client_for_does_not_retry_before_the_cooldown_elapses(tmp_path: Path) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    attempts = {"count": 0}

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        attempts["count"] += 1
        raise RuntimeError("simulated connect failure")

    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(recovery_cooldown_seconds=30.0),
        tmp_path,
        transport_factory=factory,
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        assert attempts["count"] == 1

        with pytest.raises(ToolError):
            await manager.client_for("acme")

        assert attempts["count"] == 1  # cooldown not elapsed yet -- no retry attempted


async def test_client_for_stays_failed_and_updates_next_retry_at_when_recovery_keeps_failing(
    tmp_path: Path,
) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        raise RuntimeError("simulated connect failure")

    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(recovery_cooldown_seconds=0.05),
        tmp_path,
        transport_factory=factory,
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        # Both read via `health()`, not the raw handle: `next_retry_at` is
        # stored internally on the monotonic clock and only converted to an
        # epoch-seconds estimate inside `health()` (see `ChildHandle`'s
        # docstring), so comparing a raw-handle read against a `health()`
        # read would be comparing two different clocks.
        first_health = {h["name"]: h for h in manager.health()}
        first_retry_at = first_health["acme"]["next_retry_at"]
        assert isinstance(first_retry_at, float)

        await anyio.sleep(0.1)
        with pytest.raises(ToolError):
            await manager.client_for("acme")

        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "failed"
        assert health["acme"]["recovery_attempts"] == 1
        next_retry_at = health["acme"]["next_retry_at"]
        assert isinstance(next_retry_at, float)
        assert next_retry_at > first_retry_at


async def test_recovery_closes_the_old_client_before_restarting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(recovery_cooldown_seconds=0.05),
        tmp_path,
        transport_factory=lambda site, up: FastMCPTransport(make_fake_child(site.name)),
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        handle = manager._handles["acme"]  # noqa: SLF001 - whitebox on our own fake
        old_client = handle.client
        assert old_client is not None

        closed: list[str] = []
        original_close = old_client.close

        async def _spy_close() -> None:
            closed.append("acme")
            await original_close()  # type: ignore[no-untyped-call]

        monkeypatch.setattr(old_client, "close", _spy_close)
        manager.mark_failed("acme", "simulated pipe break")

        await anyio.sleep(0.1)
        new_client = await manager.client_for("acme")

        assert closed == ["acme"]
        assert new_client is not old_client


class _EventGatedHangingTransport(ClientTransport):
    """Sets ``entered`` the instant its connect actually starts, then never
    yields a session -- lets a test deterministically wait for a recovery
    attempt to be genuinely in flight before racing a second caller against
    it, without a sleep-based guess."""

    def __init__(self, entered: anyio.Event) -> None:
        self._entered = entered

    @contextlib.asynccontextmanager
    async def connect_session(  # type: ignore[override]
        self, *, transport_options: object = None, **session_kwargs: object
    ) -> AsyncIterator[object]:
        self._entered.set()
        await anyio.sleep_forever()
        yield None  # pragma: no cover - unreachable, connect_session never yields


async def test_recovery_lock_uses_try_lock_so_a_second_caller_fails_fast(tmp_path: Path) -> None:
    """The MEDIUM finding's (c): recovery now runs inside the CALLER's own
    call-timeout budget (see mirror.py), so a second caller must not queue
    behind `handle.recovery_lock` (burning its own budget waiting on someone
    else's restart) -- it must fail fast with a clear "is recovering"
    error, and only ONE recovery attempt (one factory call) must happen."""
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    attempts = {"count": 0}
    entered = anyio.Event()

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("simulated connect failure")
        return _EventGatedHangingTransport(entered)

    manager = ChildManager(
        registry,
        UpstreamConfig(),
        # Short connect_timeout: once gated open, the recovery attempt's own
        # internal deadline resolves it (as a normal failure, not an
        # external cancellation) well within the test.
        Defaults(recovery_cooldown_seconds=0.05, connect_timeout_seconds=0.3),
        tmp_path,
        transport_factory=factory,
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        await anyio.sleep(0.1)  # past the cooldown

        results: dict[str, object] = {}

        async def _first() -> None:
            try:
                results["first"] = await manager.client_for("acme")
            except ToolError as exc:
                results["first"] = exc

        async def _second() -> None:
            await entered.wait()  # the first caller is now genuinely mid-recovery
            try:
                results["second"] = await manager.client_for("acme")
            except ToolError as exc:
                results["second"] = exc

        async with anyio.create_task_group() as tg:
            tg.start_soon(_first)
            tg.start_soon(_second)

        assert attempts["count"] == 2  # exactly one recovery attempt, not two
        assert isinstance(results["second"], ToolError)
        assert "is recovering" in str(results["second"])
        assert not manager._handles["acme"].recovery_lock.locked()  # noqa: SLF001 - whitebox


async def test_recovery_interrupted_by_the_callers_own_timeout_still_marks_the_site_failed(
    tmp_path: Path,
) -> None:
    """Recovery now runs inside the caller's own `anyio.fail_after` (see
    mirror.py's `MultiSiteProxyTool.run`) -- that timeout firing while
    `_attempt_recovery` is mid-connect must not leave the handle silently
    pointed at a half-connected client with `next_retry_at` still in the
    past, which would make the very next call immediately re-attempt the
    same stuck connect."""
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    attempts = {"count": 0}

    def factory(site: SiteConfig, up: UpstreamConfig) -> ClientTransport:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("simulated connect failure")
        return _HangingTransport()  # recovery's own connect never resolves on its own

    manager = ChildManager(
        registry,
        UpstreamConfig(),
        Defaults(recovery_cooldown_seconds=0.05, connect_timeout_seconds=30.0),
        tmp_path,
        transport_factory=factory,
    )
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        await anyio.sleep(0.1)

        before = time.time()
        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.2):
                await manager.client_for("acme")

        health = {h["name"]: h for h in manager.health()}
        assert health["acme"]["state"] == "failed"
        assert health["acme"]["recovery_attempts"] == 1
        next_retry_at = health["acme"]["next_retry_at"]
        assert isinstance(next_retry_at, float)
        assert next_retry_at > before  # not left stuck in the past
        assert not manager._handles["acme"].recovery_lock.locked()  # noqa: SLF001 - whitebox
        # A cancelled exception stringifies to "" -- a fixed, actually
        # informative reason instead of the useless "CancelledError: ".
        assert health["acme"]["last_error"] == "recovery interrupted by the caller's call timeout"


async def test_aclose_logs_shutting_down_only_once_across_repeated_calls(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = SiteRegistry([_cloud_site("acme", "ACME")])
    manager = _make_manager(registry, tmp_path, lambda site, up: FastMCPTransport(make_fake_child(site.name)))
    async with AsyncExitStack() as stack:
        await manager.start_all(stack, connect_timeout=5)
        with caplog.at_level("INFO"):
            await manager.aclose()
            await manager.aclose()

    matches = [r for r in caplog.records if "child manager shutting down" in r.message]
    assert len(matches) == 1
