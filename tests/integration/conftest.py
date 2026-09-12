"""Real-network integration fixtures: a live server process, driven by a real
fastmcp Client, against whatever ``mcp-atlassian-better`` config is actually
configured on this machine. No mocks, no fakes -- see ``test_live_sites.py``'s
module docstring for what that does and doesn't prove.

The whole suite is a no-op unless ``MCP_ATLASSIAN_BETTER_INTEGRATION=1`` -- see the
``pytestmark`` in each test module, not this file, so ``-m integration``
still *collects* the tests (and reports them skipped with the reason) rather
than silently vanishing them the way a conftest-level ``collect_ignore``
would.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.client.transports import ClientTransport, StdioTransport

from mcp_atlassian_better.config import load_config, resolve_config_path
from mcp_atlassian_better.errors import McpAtlassianBetterError
from mcp_atlassian_better.model import AppConfig

# Three real `uvx mcp-atlassian@latest` children resolving/starting cold can
# each take several seconds; generous so a slow first run doesn't flake.
CLIENT_TIMEOUT_SECONDS = 120.0


def _integration_env() -> dict[str, str]:
    """The current process's own environment, plus an explicit
    ``MCP_ATLASSIAN_BETTER_CONFIG`` so the spawned server resolves the exact same
    config file this fixture just loaded -- never left to the subprocess's
    own (potentially different) default-path resolution."""
    env = dict(os.environ)
    env.setdefault("MCP_ATLASSIAN_BETTER_CONFIG", str(resolve_config_path()))
    return env


@pytest.fixture(scope="session")
def real_config() -> AppConfig:
    try:
        return load_config()
    except McpAtlassianBetterError as exc:
        pytest.fail(
            f"MCP_ATLASSIAN_BETTER_INTEGRATION=1 but no usable config was found ({exc}); "
            "run `mcp-atlassian-better --check` first to confirm one exists and is valid."
        )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def live_client() -> AsyncIterator[Client[ClientTransport]]:
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "mcp_atlassian_better"],
        env=_integration_env(),
    )
    async with Client(
        transport,
        timeout=CLIENT_TIMEOUT_SECONDS,
        init_timeout=CLIENT_TIMEOUT_SECONDS,
        # Pinned to "legacy" for deterministic behavior in this suite -- the
        # classic initialize handshake every configured client (Claude Code,
        # Claude Desktop) already uses in practice. fastmcp Client's default
        # mode="auto" (which probes the newer `server/discover` protocol
        # before falling back) also connected fine in testing against this
        # server (3/3); "legacy" is pinned here for determinism, not to work
        # around a reproduced "auto" failure.
        mode="legacy",
    ) as client:
        yield client
