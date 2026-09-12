"""Command-line entry point: --check, --print-config, --warm, and (M2) serve."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from mcp_atlassian_better import __version__
from mcp_atlassian_better.children import minimal_env
from mcp_atlassian_better.config import default_sources, load_config
from mcp_atlassian_better.errors import McpAtlassianBetterError
from mcp_atlassian_better.logging_setup import LOGGER_NAME, collect_secrets, configure_logging
from mcp_atlassian_better.model import AppConfig, SiteConfig
from mcp_atlassian_better.secrets import redact_text
from mcp_atlassian_better.server import SHUTDOWN_WATCHDOG_SECONDS, serve
from mcp_atlassian_better.sources import ConfigSource, EnvOverlaySource

_CLOUD_MYSELF_PATH = "/rest/api/3/myself"
_SERVER_MYSELF_PATH = "/rest/api/2/myself"
_UVX_TOKENS = ("uvx",)
_UV_TOOL_RUN_TOKENS = ("uv", "tool", "run")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-atlassian-better",
        description="One MCP server for many Jira Cloud sites.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (overrides MCP_ATLASSIAN_BETTER_CONFIG and the XDG default).",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    parser.add_argument(
        "--check", action="store_true", help="Verify every configured site authenticates, then exit."
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        dest="print_config",
        help="Print the effective configuration with secrets masked, then exit.",
    )
    parser.add_argument(
        "--warm", action="store_true", help="Run the upstream command once to prime its uvx cache, then exit."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="With --warm, force uvx to re-resolve the upstream package instead of using its cache.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="With --check, exit 0 if at least one site authenticates, even if others fail.",
    )
    return parser


def _sources_for(config_path: Path | None) -> list[ConfigSource] | None:
    if config_path is None:
        return None
    return default_sources(config_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    chosen = [flag for flag in ("check", "print_config", "warm") if getattr(args, flag)]
    if len(chosen) > 1:
        parser.error("only one of --check, --print-config, --warm may be given")
    if args.refresh and not args.warm:
        parser.error("--refresh requires --warm")

    # Configured before load_config so an advisory message logged while parsing
    # (e.g. "consider api_token_env") actually reaches a handler; reconfigured
    # after with the real secrets so the redaction filter covers what follows.
    configure_logging(None, verbose=args.verbose)
    try:
        config = load_config(sources=_sources_for(args.config))
    except McpAtlassianBetterError as exc:
        # Routed through the logger (not a bare stderr write) for consistency
        # with the rest of the CLI. Note load_config failed on this path, so
        # zero secrets are registered yet and nothing here is actually
        # redacted -- this only guards a future ConfigError message once a
        # partial config load can register some secrets before failing.
        logging.getLogger(LOGGER_NAME).error("error: %s", exc)
        return 2

    configure_logging(config, verbose=args.verbose)

    if args.print_config:
        sys.stdout.write(_render_config(config))
        return 0
    if args.warm:
        return _cmd_warm(config, refresh=args.refresh)
    if args.check:
        return asyncio.run(_cmd_check(config, allow_partial=args.allow_partial))

    try:
        return _serve_with_watchdog(config, verbose=args.verbose)
    except McpAtlassianBetterError as exc:
        # e.g. SchemaConflictError raised while building the mirrored tool
        # set: a deliberate error, not a bug, so it gets the same clean
        # "error: ..." + exit 2 shape as a config-load failure, not a raw
        # traceback.
        logging.getLogger(LOGGER_NAME).error("error: %s", exc)
        return 2


def _serve_with_watchdog(config: AppConfig, *, verbose: bool) -> int:
    """Runs ``serve()`` under ``asyncio.run``, with a ``threading.Timer``
    watchdog that outlives ``serve()`` itself.

    ``serve()`` already bounds its OWN shutdown work with
    ``SHUTDOWN_WATCHDOG_SECONDS`` (see its module docstring), but that bound
    lives entirely inside the coroutine `asyncio.run` drives -- it can't cover
    `asyncio.run`'s own post-return cleanup (`_cancel_all_tasks` cancelling
    any task `serve()` leaves running, e.g. a child-transport task whose own
    subprocess teardown is still in flight). Confirmed empirically: a real
    upstream child SIGSTOP'd mid-close left the process hung well past
    `serve()` having already returned, only exiting once the child was
    resumed by hand. A `threading.Timer` runs on its own OS thread, so it
    still fires even if the very thing stuck is the event loop `asyncio.run`
    is trying to close.

    The timer is armed the moment `serve()`'s coroutine itself returns (or
    raises) -- not for the whole call -- so ordinary startup/serving time is
    never mistaken for the shutdown window this exists to bound. It fires
    with `serve()`'s OWN exit code (already known by then), matching
    `_force_exit`'s 0-if-clean/1-if-not convention instead of always
    reporting failure.
    """
    watchdog: threading.Timer | None = None

    def _arm(rc: int) -> None:
        nonlocal watchdog
        watchdog = threading.Timer(SHUTDOWN_WATCHDOG_SECONDS, os._exit, args=(rc,))
        watchdog.daemon = True
        watchdog.start()

    async def _serve_then_arm() -> int:
        try:
            rc = await serve(config, verbose=verbose)
        except BaseException:
            # A startup failure (e.g. `McpAtlassianBetterError`) still means every
            # coroutine inside `serve()` has already unwound -- the only
            # remaining risk is the SAME `asyncio.run` cleanup phase, so this
            # path needs the exact same guard; `1` is just what `_force_exit`
            # would report for "did not close cleanly", never actually
            # surfaced (the exception itself decides `main`'s return code).
            _arm(1)
            raise
        _arm(rc)
        return rc

    try:
        return asyncio.run(_serve_then_arm())
    finally:
        if watchdog is not None:
            watchdog.cancel()


def _literal_token_source(site: SiteConfig, env_var_suffix: str) -> str:
    """Where a literal (non ``*_env``) token value came from: the TOML file,
    or a ``MCP_ATLASSIAN_BETTER_SITE_<NAME>_<FIELD>`` env-overlay variable naming the
    value directly rather than pointing at another env var to read it from.

    Scans rather than reconstructing one uppercase-name guess: ``EnvOverlaySource``
    lowercases whatever case the ``<NAME>`` segment was actually written in (only
    the fixed ``MCP_ATLASSIAN_BETTER_SITE_`` prefix and ``_<FIELD>`` suffix are exact-case),
    so a real variable like ``MCP_ATLASSIAN_BETTER_SITE_acme_API_TOKEN`` must still match a
    site named ``acme``.
    """
    prefix = EnvOverlaySource.PREFIX
    suffix = f"_{env_var_suffix}"
    for key in os.environ:
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        middle = key[len(prefix) : -len(suffix)]
        if middle.lower() == site.name:
            return "env overlay"
    return "file"


def _render_config(config: AppConfig) -> str:
    lines = [
        "[defaults]",
        f"  toolset_preset = {config.defaults.toolset_preset!r}",
        f"  call_timeout_seconds = {config.defaults.call_timeout_seconds}",
        f"  connect_timeout_seconds = {config.defaults.connect_timeout_seconds}",
        f"  attachment_max_bytes = {config.defaults.attachment_max_bytes}",
        f"  recovery_cooldown_seconds = {config.defaults.recovery_cooldown_seconds}",
        f"  health_recovery_budget_seconds = {config.defaults.health_recovery_budget_seconds}",
        "",
        "[upstream]",
        f"  command = {list(config.upstream.command)!r}",
        f"  env_passthrough = {list(config.upstream.env_passthrough)!r}",
        f"  workspace_dir = {config.upstream.workspace_dir!r}",
    ]
    for site in config.sites:
        lines.append("")
        lines.append(f"[[sites]]  # {site.name}")
        lines.append(f"  source = {site.source!r}")
        lines.append(f"  url = {site.url!r}")
        lines.append(f"  key_prefixes = {list(site.key_prefixes)!r}")
        lines.append(f"  read_only = {site.read_only}")
        if site.api_token is not None:
            source = (
                f"env:{site.api_token_env}"
                if site.api_token_env
                else _literal_token_source(site, "API_TOKEN")
            )
            lines.append(f"  username = {site.username!r}")
            lines.append(f"  auth = cloud (api_token = *** [{source}])")
        else:
            source = (
                f"env:{site.personal_token_env}"
                if site.personal_token_env
                else _literal_token_source(site, "PERSONAL_TOKEN")
            )
            lines.append(f"  auth = server_dc (personal_token = *** [{source}])")
        if site.enabled_tools is not None:
            lines.append(f"  enabled_tools = {sorted(site.enabled_tools)!r}")
        if site.projects_filter is not None:
            lines.append(f"  projects_filter = {list(site.projects_filter)!r}")
    return redact_text("\n".join(lines) + "\n", collect_secrets(config))


def _refresh_insertion_index(command: Sequence[str]) -> int:
    """Where to insert ``--refresh`` — right after the ``uvx`` or ``uv tool run`` token(s)."""
    if tuple(command[: len(_UVX_TOKENS)]) == _UVX_TOKENS:
        return len(_UVX_TOKENS)
    if tuple(command[: len(_UV_TOOL_RUN_TOKENS)]) == _UV_TOOL_RUN_TOKENS:
        return len(_UV_TOOL_RUN_TOKENS)
    raise McpAtlassianBetterError(
        f"--refresh requires upstream.command to start with 'uvx' or 'uv tool run', got {list(command)!r}"
    )


def _warm_env(config: AppConfig) -> dict[str, str]:
    """A minimal, explicit child env: no ambient secrets (e.g. an API token)
    leak into a subprocess that only needs to resolve and print --help."""
    return minimal_env(config.upstream.env_passthrough)


def _cmd_warm(config: AppConfig, *, refresh: bool) -> int:
    secrets = collect_secrets(config)
    command = list(config.upstream.command)
    if refresh:
        try:
            command.insert(_refresh_insertion_index(command), "--refresh")
        except McpAtlassianBetterError as exc:
            sys.stderr.write(f"error: {redact_text(str(exc), secrets)}\n")
            return 1
    command.append("--help")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.defaults.call_timeout_seconds,
            env=_warm_env(config),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        sys.stderr.write(
            f"error: upstream command {redact_text(str(command), secrets)} timed out after {exc.timeout}s\n"
        )
        return 1
    except OSError as exc:
        sys.stderr.write(
            f"error: failed to run upstream command {redact_text(str(command), secrets)}: {exc}\n"
        )
        return 1
    if completed.returncode != 0:
        detail = redact_text(completed.stderr.strip(), secrets)
        sys.stderr.write(f"warm failed (exit {completed.returncode}): {detail}\n")
        return 1
    sys.stdout.write(f"upstream cache warmed: {redact_text(' '.join(command), secrets)}\n")
    return 0


@dataclass(frozen=True, slots=True)
class _CheckResult:
    site: SiteConfig
    ok: bool
    detail: str = ""
    display_name: str = ""
    account_id: str = ""


async def _check_site(site: SiteConfig, *, call_timeout: float, connect_timeout: float) -> _CheckResult:
    if site.personal_token is not None:
        path = _SERVER_MYSELF_PATH
        headers = {"Authorization": f"Bearer {site.personal_token.get_secret_value()}"}
        auth = None
    else:
        assert site.api_token is not None
        path = _CLOUD_MYSELF_PATH
        headers = {}
        auth = httpx.BasicAuth(site.username or "", site.api_token.get_secret_value())

    url = f"{site.url}{path}"
    timeout = httpx.Timeout(call_timeout, connect=connect_timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers, auth=auth)
    except httpx.HTTPError as exc:
        return _CheckResult(site=site, ok=False, detail=f"request failed: {exc.__class__.__name__}: {exc}")

    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            return _CheckResult(site=site, ok=False, detail="HTTP 200 but the response body was not JSON")
        return _CheckResult(
            site=site,
            ok=True,
            display_name=str(data.get("displayName", "")),
            account_id=str(data.get("accountId", data.get("key", ""))),
        )

    detail = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("errorMessages"):
        detail += ": " + "; ".join(str(m) for m in body["errorMessages"])
    return _CheckResult(site=site, ok=False, detail=detail)


async def _cmd_check(config: AppConfig, *, allow_partial: bool) -> int:
    raw_results = await asyncio.gather(
        *(
            _check_site(
                site,
                call_timeout=config.defaults.call_timeout_seconds,
                connect_timeout=config.defaults.connect_timeout_seconds,
            )
            for site in config.sites
        ),
        return_exceptions=True,
    )
    secrets = collect_secrets(config)
    results: list[_CheckResult] = []
    for site, raw in zip(config.sites, raw_results, strict=True):
        if isinstance(raw, BaseException):
            # A site whose check raised something other than httpx.HTTPError
            # (already handled inside _check_site) must not take the whole
            # table down with it.
            detail = redact_text(f"{raw.__class__.__name__}: {raw}", secrets)
            results.append(_CheckResult(site=site, ok=False, detail=detail))
        elif raw.detail:
            # _check_site's own httpx.HTTPError/error-body branches build
            # `detail` from exception text or a Jira response body, neither
            # of which goes through a redacting logger on this path.
            raw = _CheckResult(
                site=raw.site,
                ok=raw.ok,
                detail=redact_text(raw.detail, secrets),
                display_name=raw.display_name,
                account_id=raw.account_id,
            )
            results.append(raw)
        else:
            results.append(raw)
    sys.stdout.write(_render_check_table(results))
    all_ok = all(r.ok for r in results)
    any_ok = any(r.ok for r in results)
    if all_ok or (allow_partial and any_ok):
        return 0
    return 1


def _render_check_table(results: Sequence[_CheckResult]) -> str:
    rows = []
    for result in results:
        site = result.site
        host = site.url.removeprefix("https://")
        auth_mode = "bearer" if site.personal_token is not None else "basic"
        prefixes = ", ".join(site.key_prefixes)
        status = f"OK {result.display_name} ({result.account_id})" if result.ok else f"FAILED {result.detail}"
        rows.append((site.name, host, auth_mode, status, site.source, prefixes))

    site_width = max(4, *(len(r[0]) for r in rows))
    host_width = max(4, *(len(r[1]) for r in rows))
    auth_width = max(4, *(len(r[2]) for r in rows))
    status_width = max(6, *(len(r[3]) for r in rows))
    source_width = max(6, *(len(r[4]) for r in rows))

    header = (
        f"{'SITE':<{site_width}} {'HOST':<{host_width}} {'AUTH':<{auth_width}} "
        f"{'STATUS':<{status_width}} {'SOURCE':<{source_width}} {'PREFIXES'}"
    )
    lines = [header, "-" * len(header)]
    for name, host, auth_mode, status, source, prefixes in rows:
        lines.append(
            f"{name:<{site_width}} {host:<{host_width}} {auth_mode:<{auth_width}} "
            f"{status:<{status_width}} {source:<{source_width}} {prefixes}"
        )
    return "\n".join(lines) + "\n"
