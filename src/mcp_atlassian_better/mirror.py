"""Builds the ONE mirrored tool set: each upstream tool gets an optional
``site`` parameter and forwards its call to the right child."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import anyio
import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool, ToolResult
from mcp import MCPError
from pydantic import PrivateAttr

from mcp_atlassian_better.children import ChildManager
from mcp_atlassian_better.errors import SchemaConflictError, SiteResolutionError
from mcp_atlassian_better.registry import SiteRegistry, resolve_site
from mcp_atlassian_better.site_policy import enforce_site_policy
from mcp_atlassian_better.tools_meta import WRAPPER_OWNED_TOOLS

_logger = logging.getLogger(__name__)

SITE_PARAM = "site"

_CONNECTION_ERROR_TYPES = (anyio.ClosedResourceError, anyio.BrokenResourceError)


def _is_connection_failure(exc: BaseException) -> bool:
    """Whether ``exc`` looks like the child's pipe/session breaking mid-call
    (as opposed to a plain application error), the trigger for marking a site
    ``failed`` in ``jira_sites`` -- restart is out of scope (M4), but a dead
    site should stop being reported ``healthy``."""
    if isinstance(exc, _CONNECTION_ERROR_TYPES):
        return True
    if isinstance(exc, MCPError):
        text = str(exc).lower()
        return "closed" in text or "connection" in text
    return False


def augment_input_schema(schema: Mapping[str, Any], registry: SiteRegistry) -> dict[str, Any]:
    """Adds an optional ``site`` property to an upstream tool's JSON schema.

    Never touches ``required``: a tool call omitting ``site`` is always valid
    (the resolver infers it from an issue/project key, or raises a clear
    error naming every prefix). Raises if upstream already defines a ``site``
    property — silently overwriting it would surprise nobody more than us.
    """
    augmented = copy.deepcopy(dict(schema))
    augmented.setdefault("type", "object")
    properties = augmented.setdefault("properties", {})
    if SITE_PARAM in properties:
        raise SchemaConflictError(
            f"upstream tool schema already defines a '{SITE_PARAM}' property; refusing to mirror it"
        )
    properties[SITE_PARAM] = {
        "type": "string",
        "enum": [site.name for site in registry.sites],
        "description": (
            "Jira site to target. Omit to infer it from the issue key prefix. "
            f"Prefixes — {registry.prefix_table()}. "
            "Required when no issue key is in the arguments (for example jira_search)."
        ),
    }
    return augmented


def augment_description(description: str | None, registry: SiteRegistry) -> str:
    suffix = f"Configured Jira sites — {registry.prefix_table()}."
    if not description:
        return suffix
    return f"{description}\n\n{suffix}"


class MultiSiteProxyTool(Tool):
    """A mirrored tool: resolves ``site`` client-side, then forwards the call
    to that site's child via ``call_tool_mcp`` (never through the child's own
    routing — the child has no idea other sites exist)."""

    KEY_PREFIX: ClassVar[str] = "tool"

    _manager: ChildManager = PrivateAttr()
    _registry: SiteRegistry = PrivateAttr()

    @classmethod
    def from_mcp_tool(
        cls,
        manager: ChildManager,
        registry: SiteRegistry,
        mcp_tool: mcp_types.Tool,
        timeout: float,
    ) -> MultiSiteProxyTool:
        tags: set[str] = set()
        if mcp_tool.meta:
            fastmcp_meta = mcp_tool.meta.get("fastmcp")
            if isinstance(fastmcp_meta, dict):
                raw_tags = fastmcp_meta.get("tags")
                if isinstance(raw_tags, list):
                    tags = {str(tag) for tag in raw_tags}

        instance = cls(
            name=mcp_tool.name,
            title=mcp_tool.title,
            description=augment_description(mcp_tool.description, registry),
            parameters=augment_input_schema(mcp_tool.input_schema, registry),
            output_schema=mcp_tool.output_schema,
            annotations=mcp_tool.annotations,
            tags=tags,
            timeout=timeout,
        )
        instance._manager = manager
        instance._registry = registry
        return instance

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        args = dict(arguments)
        explicit_site = args.pop(SITE_PARAM, None)
        try:
            resolution = resolve_site(self._registry, args, explicit=explicit_site, tool_name=self.name)
        except SiteResolutionError as exc:
            raise ToolError(str(exc)) from exc

        site = resolution.site
        # Belt, scoped to `enabled_tools` only: that's normally enforced for
        # free by the child process itself (its own ENABLED_TOOLS env var --
        # see `children.build_child_env`), but calling `enforce_site_policy`
        # here too means the restriction still holds even if a child
        # misbehaves -- serves a tool it was never supposed to (the
        # ENABLED_TOOLS empty-string "no filter" upstream bug this same round
        # fixed is exactly that kind of misbehavior).
        #
        # `is_write=False` here is deliberate, NOT "this tool happens to be
        # read-only": it means this call can never trip `enforce_site_policy`'s
        # `read_only` branch. `read_only` enforcement for a mirrored call
        # stays entirely the child's job (READ_ONLY_MODE) plus the existing
        # post-hoc hint appended below on a child error -- verified against
        # a live Jira Cloud site that every CURATED read tool sets
        # `readOnlyHint=True` explicitly, but the "all" toolset preset can
        # mirror uncurated (e.g. agile/board) tools whose annotations aren't
        # verified, and pre-emptively refusing those here on a read_only site
        # would be a real, unproven regression risk -- not something this
        # round's finding (which only asked for an `enabled_tools` belt)
        # asked for. `issue_key=None` because at this layer we only have the
        # raw (pre-routing) arguments, not a resolved issue key worth
        # checking against `projects_filter` -- that check stays
        # wrapper-tool-only.
        enforce_site_policy(site, self.name, is_write=False, issue_key=None)

        assert self.timeout is not None, f"mirrored tool '{self.name}' was built without a timeout"
        log_path = self._manager.log_path(site.name)
        try:
            # `client_for` runs INSIDE this scope, not before it: a failed
            # site's recovery (closing the old client, connecting a new one,
            # probing liveness) can itself take several seconds, and that
            # time must count against this call's own `call_timeout_seconds`
            # budget rather than running for free ahead of it -- otherwise a
            # caller's effective timeout was `call_timeout_seconds +
            # recovery time`, up to `connect_timeout_seconds` more (see
            # `children.ChildManager.client_for`'s docstring).
            # `generation` (captured only once `client_for` has actually
            # returned a client) lets `mark_timeout`/`mark_failed` below
            # no-op if a recovery has ALREADY replaced this handle's client
            # by the time this call fails -- otherwise a slow, stale
            # in-flight call could clobber a newer, healthy client's state.
            # Stays `None` if `client_for` itself is what the timeout/failure
            # hit (recovery still in flight, no client obtained yet) -- in
            # that case `_attempt_recovery` has already recorded its own,
            # more specific failure reason, so `run` must NOT also call
            # `mark_timeout`/`mark_failed` here: doing so would overwrite
            # that reason and, for a timeout, inflate the site's consecutive-
            # timeout counter for a call that was never actually made.
            generation: int | None = None
            with anyio.fail_after(self.timeout):
                client = await self._manager.client_for(site.name)
                generation = self._manager.generation(site.name)
                result = await client.call_tool_mcp(self.name, args)
        except TimeoutError as exc:
            if generation is not None:
                self._manager.mark_timeout(
                    site.name, f"{self.name} timed out after {self.timeout}s", generation=generation
                )
            raise ToolError(
                f"[site={site.name}] {self.name} timed out after {self.timeout}s. Child log: {log_path}"
            ) from exc
        except ToolError:
            # `client_for` (or the recovery attempt it triggers) can raise
            # its own already-well-shaped `ToolError` directly -- "site is
            # unavailable: ..." or "is recovering; retry shortly". Re-raise
            # as-is rather than double-wrapping it in the generic-failure
            # shape below (which would otherwise render as
            # "... failed: ToolError: [site=x] ...").
            raise
        except Exception as exc:
            # The child died or the connection otherwise broke mid-call (not a
            # timeout, not a tool-level error the child reported normally) --
            # give it the same [site=] shape and a pointer to its log instead
            # of letting a raw MCPError/connection exception escape unshaped.
            reason = f"{exc.__class__.__name__}: {exc}"
            if _is_connection_failure(exc):
                self._manager.mark_failed(site.name, reason, generation=generation)
            redacted = self._manager.redact(reason)
            raise ToolError(
                f"[site={site.name}] {self.name} failed: {redacted}. Child log: {log_path}"
            ) from exc

        # The child answered -- whether the tool itself errored or not, the
        # round trip completed, so any earlier consecutive-timeout streak is
        # over.
        self._manager.mark_success(site.name)

        if result.is_error:
            text = "\n".join(
                block.text for block in result.content if isinstance(block, mcp_types.TextContent)
            )
            text = self._manager.redact(text)
            hint = ""
            # Most upstream write tools set NO readOnlyHint at all (verified
            # against mcp-atlassian 0.23.1: 23 of 25), so treating that as
            # "known read-only" left the hint dead for nearly every write
            # tool. Only a tool the child explicitly marked readOnlyHint=True
            # is exempt.
            if site.read_only and (self.annotations is None or self.annotations.read_only_hint is not True):
                hint = f" (site '{site.name}' is configured read_only = true)"
            raise ToolError(f"[site={site.name}] {text}{hint}")

        return ToolResult(
            content=result.content,
            structured_content=result.structured_content,
            meta={
                **(result.meta or {}),
                "mcp-atlassian-better/site": site.name,
                "mcp-atlassian-better/site_reason": resolution.reason,
            },
        )


def build_mirrored_tools(
    mcp_tools: Sequence[mcp_types.Tool],
    manager: ChildManager,
    registry: SiteRegistry,
    allowlist: frozenset[str] | None,
    timeout: float,
) -> list[MultiSiteProxyTool]:
    """One ``MultiSiteProxyTool`` per discovered tool that isn't wrapper-owned
    and (when ``allowlist`` is given) is in it. ``allowlist=None`` mirrors
    every discovered tool (the "all" toolset preset)."""
    mirrored = []
    discovered_names = {tool.name for tool in mcp_tools}
    for mcp_tool in mcp_tools:
        if mcp_tool.name in WRAPPER_OWNED_TOOLS:
            continue
        if allowlist is not None and mcp_tool.name not in allowlist:
            continue
        mirrored.append(MultiSiteProxyTool.from_mcp_tool(manager, registry, mcp_tool, timeout))

    if allowlist is not None:
        for name in sorted((allowlist - WRAPPER_OWNED_TOOLS) - discovered_names):
            _logger.warning("tool '%s' is allowlisted but no healthy child advertised it", name)

    return mirrored


class LateMirror:
    """Mirrors newly-discovered tools onto the already-running server AFTER
    startup -- the one case that can't wait for a restart: every configured
    site failed at startup, so ``discover_tools()`` returned nothing and
    ``build_mirrored_tools`` mirrored zero tools. From there, ``client_for``
    -- the only other path that ever drives a failed site's recovery -- is
    never reached again either (nothing calls a tool that doesn't exist), so
    without this the server would serve `jira_sites` and the attachment
    tools forever with no way back.

    ``wrapper_tools.build_jira_sites_tool`` calls ``after_recovery`` on
    every ``jira_sites`` invocation, right after
    ``ChildManager.recover_failed_sites()``. A no-op once something has
    already been mirrored, whether that happened at startup or via a
    previous call here.
    """

    def __init__(
        self,
        mcp: FastMCP,
        manager: ChildManager,
        registry: SiteRegistry,
        allowlist: frozenset[str] | None,
        timeout: float,
        *,
        already_mirrored: bool = False,
    ) -> None:
        self._mcp = mcp
        self._manager = manager
        self._registry = registry
        self._allowlist = allowlist
        self._timeout = timeout
        # `already_mirrored=True` when the caller already added tools from
        # this SAME discovery pipeline once (the ordinary startup path in
        # `server.serve`) -- without this, the first `jira_sites` call after
        # a successful startup would immediately re-discover and re-add every
        # already-mirrored tool a second time. `mcp.add_tool` would NOT raise
        # on that (fastmcp 4.0.3's default `on_duplicate="warn"` replaces the
        # existing tool and just logs a warning), but it's still pointless
        # rediscovery-and-replace work and log spam on every single call.
        self._mirrored = already_mirrored
        # Guards against two concurrent `jira_sites` calls both observing
        # `_mirrored=False` and both running a whole `discover_tools()` +
        # rebuild for nothing -- not (as an earlier version of this comment
        # claimed) to stop `mcp.add_tool` from crashing on a duplicate name;
        # it wouldn't (see above).
        self._lock = anyio.Lock()

    async def after_recovery(self, ctx: Context) -> str | None:
        """Returns a note for ``jira_sites`` to surface only when tools WERE
        newly mirrored just now but the connected client couldn't be told
        its tool list changed (it may need a manual re-list); ``None`` in
        every other case, including "nothing to do".

        Uses try-lock semantics (``acquire_nowait``), consistent with
        ``ChildManager._maybe_recover``: this runs inside `jira_sites`'s own
        call budget (its caller wraps this call in the SAME
        ``anyio.move_on_after(health_recovery_budget_seconds)`` scope it
        already used for recovery -- see ``wrapper_tools.build_jira_sites_tool``),
        so a second, concurrent caller queuing behind the lock would burn its
        own budget waiting on someone else's discovery instead of getting a
        fast, clear answer -- it just returns ``None`` and the NEXT
        `jira_sites` call retries. ``discover_tools()`` itself is ALSO bounded
        by ``self._timeout`` (the M4a MEDIUM 2 finding, call_timeout_seconds
        by default) as an inner, independent backstop -- without that, one
        slow/hanging child's ``list_tools()`` could stall this call for as
        long as THAT takes even on a caller that isn't going through
        `jira_sites`'s own budget at all. Whichever of the two is smaller is
        what actually bounds a `jira_sites` call in practice.
        """
        if self._mirrored:
            return None
        try:
            self._lock.acquire_nowait()
        except anyio.WouldBlock:
            return None
        try:
            if self._mirrored:
                return None
            with anyio.move_on_after(self._timeout) as scope:
                tools = await self._manager.discover_tools()
            if scope.cancelled_caught:
                _logger.warning(
                    "late-mirror discover_tools() timed out after %ss; leaving unmirrored "
                    "for a later jira_sites call to retry",
                    self._timeout,
                )
                return None
            mirrored = build_mirrored_tools(
                tools, self._manager, self._registry, self._allowlist, self._timeout
            )
            if not mirrored:
                # Nothing healthy enough yet to mirror anything real --
                # leave `_mirrored` false so a later call can retry.
                return None
            for tool in mirrored:
                self._mcp.add_tool(tool)
            self._mirrored = True
        finally:
            self._lock.release()
        try:
            await ctx.session.send_tool_list_changed()
        except Exception:  # noqa: BLE001 - best-effort; an older/simpler client may not support this
            return (
                "tools were mirrored after recovery, but the connected client could not be "
                "notified of the change -- it may need to re-list tools to see them"
            )
        return None
