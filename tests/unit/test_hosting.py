"""Hosting knobs: forced read-only, the upstream-command env override, the
transport/host/port/allowed-hosts settings, health routes, and which tools
stay visible when every site is read-only or the transport is HTTP."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp_types
import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool

from mcp_atlassian_better.cli import _http_settings, build_parser, main
from mcp_atlassian_better.config import load_config
from mcp_atlassian_better.errors import ConfigError
from mcp_atlassian_better.mirror import LateMirror, build_mirrored_tools
from mcp_atlassian_better.model import AppConfig
from mcp_atlassian_better.registry import SiteRegistry
from mcp_atlassian_better.server import HealthRoutes, HttpSettings, visible_wrapper_tools
from mcp_atlassian_better.sources import DropInSitesSource, EnvOverlaySource, TomlFileConfigSource

TOML = """
[defaults]
username = "you@example.com"
api_token = "file-token"

[upstream]
command = ["uvx", "mcp-atlassian@latest"]

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]
read_only = false

[[sites]]
name = "scoped"
url = "https://api.atlassian.com/ex/jira/00000000-0000-0000-0000-000000000000"
key_prefixes = ["PROJ"]
"""


def _load(tmp_path: Path, environ: dict[str, str], *, toml: str = TOML) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(toml)
    path.chmod(0o600)
    return load_config(
        sources=[
            DropInSitesSource(tmp_path / "sites.d"),
            TomlFileConfigSource(path),
            EnvOverlaySource(environ),
        ]
    )


def test_force_read_only_env_beats_toml_read_only_false(tmp_path: Path) -> None:
    config = _load(tmp_path, {"MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY": "true"})
    assert config.defaults.force_read_only is True
    assert all(site.read_only for site in config.sites)


def test_force_read_only_env_beats_a_per_site_env_read_only_false(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        {
            "MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY": "1",
            "MCP_ATLASSIAN_BETTER_SITE_ACME_READ_ONLY": "false",
        },
    )
    assert all(site.read_only for site in config.sites)


def test_force_read_only_env_beats_a_drop_in_site(tmp_path: Path) -> None:
    drop_in = tmp_path / "sites.d"
    drop_in.mkdir()
    (drop_in / "extra.toml").write_text(
        '[[sites]]\nname = "extra"\nurl = "https://extra.atlassian.net"\n'
        'key_prefixes = ["EXTRA"]\nread_only = false\n'
    )
    config = _load(tmp_path, {"MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY": "yes"})
    assert {site.name for site in config.sites} == {"acme", "scoped", "extra"}
    assert all(site.read_only for site in config.sites)


def test_force_read_only_in_toml_defaults(tmp_path: Path) -> None:
    toml = TOML.replace("[defaults]\n", "[defaults]\nforce_read_only = true\n", 1)
    config = _load(tmp_path, {}, toml=toml)
    assert all(site.read_only for site in config.sites)


def test_without_force_read_only_sites_keep_their_own_setting(tmp_path: Path) -> None:
    config = _load(tmp_path, {})
    assert config.defaults.force_read_only is False
    assert not any(site.read_only for site in config.sites)


def test_force_read_only_env_rejects_a_non_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY"):
        _load(tmp_path, {"MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY": "maybe"})


def test_force_read_only_toml_rejects_a_non_boolean(tmp_path: Path) -> None:
    toml = TOML.replace("[defaults]\n", '[defaults]\nforce_read_only = "true"\n', 1)
    with pytest.raises(ConfigError, match="force_read_only"):
        _load(tmp_path, {}, toml=toml)


def test_upstream_command_env_overrides_the_toml_command(tmp_path: Path) -> None:
    config = _load(tmp_path, {"MCP_ATLASSIAN_BETTER_UPSTREAM_COMMAND": "/opt/upstream/bin/mcp-atlassian"})
    assert config.upstream.command == ("/opt/upstream/bin/mcp-atlassian",)


def test_upstream_command_env_is_shell_split(tmp_path: Path) -> None:
    command = "'/opt/my dir/python' -m mcp_atlassian"
    config = _load(tmp_path, {"MCP_ATLASSIAN_BETTER_UPSTREAM_COMMAND": command})
    assert config.upstream.command == ("/opt/my dir/python", "-m", "mcp_atlassian")


def test_upstream_command_env_rejects_bad_quoting(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="MCP_ATLASSIAN_BETTER_UPSTREAM_COMMAND"):
        _load(tmp_path, {"MCP_ATLASSIAN_BETTER_UPSTREAM_COMMAND": "'unterminated"})


def test_blank_global_env_values_are_ignored(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        {"MCP_ATLASSIAN_BETTER_UPSTREAM_COMMAND": "  ", "MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY": ""},
    )
    assert config.upstream.command == ("uvx", "mcp-atlassian@latest")
    assert config.defaults.force_read_only is False


def test_scoped_api_base_url_keeps_its_path(tmp_path: Path) -> None:
    config = _load(tmp_path, {})
    scoped = next(site for site in config.sites if site.name == "scoped")
    assert scoped.url == "https://api.atlassian.com/ex/jira/00000000-0000-0000-0000-000000000000"


def test_print_config_shows_force_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(TOML)
    path.chmod(0o600)
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY", "true")
    assert main(["--config", str(path), "--print-config"]) == 0
    out = capsys.readouterr().out
    assert "force_read_only = True" in out
    assert "read_only = False" not in out
    assert "file-token" not in out


def _settings(argv: list[str]) -> HttpSettings | None:
    parser = build_parser()
    return _http_settings(parser, parser.parse_args(argv))


def _clear_transport_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MCP_ATLASSIAN_BETTER_TRANSPORT",
        "MCP_ATLASSIAN_BETTER_HOST",
        "MCP_ATLASSIAN_BETTER_PORT",
        "MCP_ATLASSIAN_BETTER_ALLOWED_HOSTS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_transport_is_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_transport_env(monkeypatch)
    assert _settings([]) is None


def test_transport_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_transport_env(monkeypatch)
    settings = _settings(
        ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000", "--allowed-hosts", "a, b:*"]
    )
    assert settings == HttpSettings(host="0.0.0.0", port=9000, allowed_hosts=("a", "b:*"))


def test_transport_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_transport_env(monkeypatch)
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_PORT", "8123")
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_ALLOWED_HOSTS", "svc.cluster.local:8123")
    assert _settings([]) == HttpSettings(host="0.0.0.0", port=8123, allowed_hosts=("svc.cluster.local:8123",))


def test_transport_flag_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_transport_env(monkeypatch)
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_TRANSPORT", "streamable-http")
    assert _settings(["--transport", "stdio"]) is None


def test_http_defaults_bind_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_transport_env(monkeypatch)
    assert _settings(["--transport", "streamable-http"]) == HttpSettings()
    assert HttpSettings().host == "127.0.0.1"


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("MCP_ATLASSIAN_BETTER_TRANSPORT", "sse"),
        ("MCP_ATLASSIAN_BETTER_PORT", "eighty"),
        ("MCP_ATLASSIAN_BETTER_PORT", "70000"),
    ],
)
def test_bad_transport_env_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, env: str, value: str) -> None:
    _clear_transport_env(monkeypatch)
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_TRANSPORT", "streamable-http")
    monkeypatch.setenv(env, value)
    with pytest.raises(SystemExit) as exc_info:
        _settings([])
    assert exc_info.value.code == 2


def test_bad_transport_flag_is_a_usage_error() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--transport", "sse"])


def _tool(name: str) -> Tool:
    def fn() -> None:
        return None

    return Tool.from_function(fn, name=name)


WRAPPER_NAMES = [
    "jira_list_attachments",
    "jira_download_attachments",
    "jira_upload_attachments",
    "jira_delete_comment",
]


def test_wrapper_tools_all_visible_on_stdio_with_a_writable_site() -> None:
    tools = visible_wrapper_tools([_tool(n) for n in WRAPPER_NAMES], all_read_only=False, http=False)
    assert [t.name for t in tools] == WRAPPER_NAMES


def test_wrapper_write_tools_hidden_when_every_site_is_read_only() -> None:
    tools = visible_wrapper_tools([_tool(n) for n in WRAPPER_NAMES], all_read_only=True, http=False)
    assert [t.name for t in tools] == ["jira_list_attachments", "jira_download_attachments"]


def test_wrapper_disk_tools_hidden_over_http() -> None:
    tools = visible_wrapper_tools([_tool(n) for n in WRAPPER_NAMES], all_read_only=False, http=True)
    assert [t.name for t in tools] == ["jira_list_attachments", "jira_delete_comment"]


def test_only_list_attachments_survives_http_and_read_only() -> None:
    tools = visible_wrapper_tools([_tool(n) for n in WRAPPER_NAMES], all_read_only=True, http=True)
    assert [t.name for t in tools] == ["jira_list_attachments"]


def _mcp_tool(name: str, *, read_only_hint: bool | None, tags: list[str] | None = None) -> mcp_types.Tool:
    annotations = None if read_only_hint is None else mcp_types.ToolAnnotations(read_only_hint=read_only_hint)
    meta: dict[str, Any] | None = {"fastmcp": {"tags": tags}} if tags is not None else None
    return mcp_types.Tool(
        name=name, input_schema={"type": "object", "properties": {}}, annotations=annotations, _meta=meta
    )


class _NoManager:
    pass


def test_read_only_only_mirror_drops_writes_and_unannotated_tools(tmp_path: Path) -> None:
    config = _load(tmp_path, {})
    registry = SiteRegistry(config.sites)
    discovered = [
        _mcp_tool("jira_get_issue", read_only_hint=True),
        _mcp_tool("jira_create_issue", read_only_hint=None, tags=["write"]),
        _mcp_tool("jira_tagged_write_claiming_read", read_only_hint=True, tags=["write"]),
        _mcp_tool("jira_unannotated", read_only_hint=None),
        _mcp_tool("jira_explicit_write", read_only_hint=False),
    ]
    manager: Any = _NoManager()
    all_tools = build_mirrored_tools(discovered, manager, registry, None, 5.0)
    assert len(all_tools) == 5
    read_only = build_mirrored_tools(discovered, manager, registry, None, 5.0, read_only_only=True)
    assert [t.name for t in read_only] == ["jira_get_issue"]


class _FakeHealth:
    def __init__(self, states: list[str], *, recovers_to: list[str] | None = None) -> None:
        self._states = states
        self._recovers_to = recovers_to
        self.recover_calls = 0

    async def recover_failed_sites(self) -> list[str]:
        self.recover_calls += 1
        if self._recovers_to is not None:
            self._states = self._recovers_to
        return []

    def health(self) -> list[dict[str, object]]:
        return [{"name": f"s{i}", "state": state} for i, state in enumerate(self._states)]


class _HangingRecovery(_FakeHealth):
    async def recover_failed_sites(self) -> list[str]:
        await anyio.sleep_forever()
        return []


async def _inner_app(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 418, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


async def _get(app: Any, path: str, method: str = "GET") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://10.0.0.9:8000") as client:
        return await client.request(method, path)


async def test_readyz_is_503_until_a_site_is_healthy() -> None:
    manager: Any = _FakeHealth(["failed", "recovering"])
    response = await _get(HealthRoutes(_inner_app, manager), "/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "healthy_sites": 0,
        "sites": 2,
        "tools_mirrored": True,
    }


async def test_readyz_is_200_with_one_healthy_site() -> None:
    manager: Any = _FakeHealth(["failed", "healthy"])
    response = await _get(HealthRoutes(_inner_app, manager), "/readyz")
    assert response.status_code == 200
    assert response.json()["healthy_sites"] == 1


async def test_healthz_is_200_even_with_no_healthy_site() -> None:
    manager: Any = _FakeHealth(["failed"])
    response = await _get(HealthRoutes(_inner_app, manager), "/healthz")
    assert response.status_code == 200


async def test_other_paths_and_methods_reach_the_mcp_app() -> None:
    manager: Any = _FakeHealth(["healthy"])
    app = HealthRoutes(_inner_app, manager)
    assert (await _get(app, "/mcp")).status_code == 418
    assert (await _get(app, "/healthz", method="POST")).status_code == 418


async def test_readyz_drives_recovery_so_a_pod_that_started_unready_recovers() -> None:
    manager: Any = _FakeHealth(["failed"], recovers_to=["healthy"])
    app = HealthRoutes(_inner_app, manager)
    response = await _get(app, "/readyz")
    assert manager.recover_calls == 1
    assert response.status_code == 200


async def test_healthz_does_not_drive_recovery() -> None:
    manager: Any = _FakeHealth(["failed"], recovers_to=["healthy"])
    await _get(HealthRoutes(_inner_app, manager), "/healthz")
    assert manager.recover_calls == 0


async def test_readyz_waits_only_briefly_for_a_slow_recovery() -> None:
    manager: Any = _HangingRecovery(["failed"])
    app = HealthRoutes(_inner_app, manager, recovery_wait_seconds=0.05)
    with anyio.fail_after(5):
        response = await _get(app, "/readyz")
    assert response.status_code == 503


class _FakeLateMirror:
    def __init__(self, *, mirrors_on_recovery: bool) -> None:
        self.mirrored = False
        self._mirrors_on_recovery = mirrors_on_recovery
        self.calls: list[object] = []

    async def after_recovery(self, ctx: object) -> str | None:
        self.calls.append(ctx)
        if self._mirrors_on_recovery:
            self.mirrored = True
        return None


async def test_readyz_is_503_while_a_healthy_site_has_no_mirrored_tools() -> None:
    manager: Any = _FakeHealth(["healthy"])
    late: Any = _FakeLateMirror(mirrors_on_recovery=False)
    response = await _get(HealthRoutes(_inner_app, manager, late_mirror=late), "/readyz")
    assert response.status_code == 503
    assert response.json()["tools_mirrored"] is False
    assert late.calls == [None]


async def test_readyz_mirrors_late_tools_and_then_reports_ready() -> None:
    manager: Any = _FakeHealth(["failed"], recovers_to=["healthy"])
    late: Any = _FakeLateMirror(mirrors_on_recovery=True)
    response = await _get(HealthRoutes(_inner_app, manager, late_mirror=late), "/readyz")
    assert response.status_code == 200
    assert response.json()["tools_mirrored"] is True


async def test_readyz_is_503_and_skips_recovery_once_shutdown_began() -> None:
    manager: Any = _FakeHealth(["healthy"])
    app = HealthRoutes(_inner_app, manager, is_stopping=lambda: True)
    response = await _get(app, "/readyz")
    assert response.status_code == 503
    assert manager.recover_calls == 0


class _StaticDiscoveryManager:
    def __init__(self, tools: list[mcp_types.Tool]) -> None:
        self._tools = tools
        self.calls = 0

    async def discover_tools(self) -> list[mcp_types.Tool]:
        self.calls += 1
        return self._tools


async def test_late_mirror_counts_as_done_when_filters_drop_every_discovered_tool(tmp_path: Path) -> None:
    config = _load(tmp_path, {})
    manager: Any = _StaticDiscoveryManager(
        [_mcp_tool("jira_create_issue", read_only_hint=None, tags=["write"])]
    )
    late = LateMirror(FastMCP("t"), manager, SiteRegistry(config.sites), None, 5.0, read_only_only=True)
    assert await late.after_recovery(None) is None
    assert late.mirrored is True
    await late.after_recovery(None)
    assert manager.calls == 1


async def test_late_mirror_retries_when_discovery_finds_nothing(tmp_path: Path) -> None:
    config = _load(tmp_path, {})
    manager: Any = _StaticDiscoveryManager([])
    late = LateMirror(FastMCP("t"), manager, SiteRegistry(config.sites), None, 5.0)
    await late.after_recovery(None)
    assert late.mirrored is False
