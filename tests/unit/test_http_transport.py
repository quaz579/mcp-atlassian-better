"""Real-subprocess proof of the streamable-http transport: the server binds,
answers health probes regardless of Host, rejects an unknown Host on the MCP
endpoint, serves a forced-read-only tool list over a real MCP client, and
still tears its child down on SIGTERM."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx
import pytest
from fastmcp import Client

from mcp_atlassian_better.server import SHUTDOWN_WATCHDOG_SECONDS

_MAX_SHUTDOWN_SECONDS = SHUTDOWN_WATCHDOG_SECONDS + 5.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_upstream_script(tmp_path: Path) -> Path:
    script = tmp_path / "upstream.py"
    script.write_text(
        "import os, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('0.0.0-fake')\n"
        "    sys.exit(0)\n"
        "with open(sys.argv[1], 'w') as f:\n"
        "    f.write(str(os.getpid()))\n"
        "from fastmcp import FastMCP\n"
        "from mcp_types import ToolAnnotations\n"
        "mcp = FastMCP('fake-upstream')\n"
        "@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))\n"
        "def jira_get_issue(issue_key: str) -> dict:\n"
        "    return {'issue_key': issue_key, 'read_only_mode': os.environ.get('READ_ONLY_MODE')}\n"
        "@mcp.tool(tags={'write'})\n"
        "def jira_create_issue(project_key: str, summary: str) -> dict:\n"
        "    return {'created': True}\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    return script


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[defaults]
username = "you@example.com"
api_token_env = "FAKE_JIRA_TOKEN"
toolset_preset = "all"
connect_timeout_seconds = 30
call_timeout_seconds = 10

[upstream]
command = ["uvx", "mcp-atlassian@latest"]

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]
read_only = false
"""
    )
    config_path.chmod(0o600)
    return config_path


def _wait_until_live(port: int, proc: subprocess.Popen[bytes], *, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early with {proc.returncode}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise AssertionError("server never answered /healthz")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _list_and_call(port: int) -> tuple[set[str], str]:
    async with Client(f"http://127.0.0.1:{port}/mcp") as client:
        names = {tool.name for tool in await client.list_tools()}
        result = await client.call_tool("jira_get_issue", {"issue_key": "ACME-1"})
        text = "".join(getattr(block, "text", "") for block in result.content)
    return names, text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_streamable_http_serves_a_forced_read_only_tool_list(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    upstream = _write_upstream_script(tmp_path)
    config_path = _write_config(tmp_path)
    port = _free_port()
    env = {
        **os.environ,
        "FAKE_JIRA_TOKEN": "fake-token-value",
        "MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY": "true",
        "MCP_ATLASSIAN_BETTER_UPSTREAM_COMMAND": f"{sys.executable} {upstream} {pid_file}",
        "MCP_ATLASSIAN_BETTER_TRANSPORT": "streamable-http",
        "MCP_ATLASSIAN_BETTER_PORT": str(port),
        "MCP_ATLASSIAN_BETTER_ALLOWED_HOSTS": "mcp.example.internal:*",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_atlassian_better", "--config", str(config_path)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_until_live(port, proc)
        child_pid = int(pid_file.read_text())

        ready = httpx.get(f"http://127.0.0.1:{port}/readyz", headers={"Host": "10.1.2.3:8000"})
        assert ready.status_code == 200
        assert ready.json()["healthy_sites"] == 1
        live = httpx.get(f"http://127.0.0.1:{port}/healthz", headers={"Host": "10.1.2.3:8000"})
        assert live.status_code == 200

        rejected = httpx.post(f"http://127.0.0.1:{port}/mcp", headers={"Host": "evil.example"}, json={})
        assert rejected.status_code == 421
        allowed = httpx.post(
            f"http://127.0.0.1:{port}/mcp", headers={"Host": "mcp.example.internal:8000"}, json={}
        )
        assert allowed.status_code != 421

        names, text = anyio.run(_list_and_call, port)
        assert "jira_get_issue" in names
        assert "jira_sites" in names
        assert "jira_list_attachments" in names
        assert "jira_create_issue" not in names
        assert "jira_upload_attachments" not in names
        assert "jira_delete_comment" not in names
        assert "jira_download_attachments" not in names
        assert '"read_only_mode":"true"' in text.replace(" ", "")

        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=_MAX_SHUTDOWN_SECONDS)
        assert time.monotonic() - started < _MAX_SHUTDOWN_SECONDS
        assert proc.returncode == 0
        assert proc.stderr is not None
        assert "Traceback" not in proc.stderr.read().decode()
        deadline = time.monotonic() + 5
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _pid_alive(child_pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
