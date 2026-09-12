"""Logging setup: stderr + a rotating file under XDG state, never stdout.

Redaction is enforced at the LOGGER level, not only via handler formatters.
``configure_logging`` attaches a ``RedactingFilter`` directly to the
``mcp_atlassian_better``, ``httpx``, ``httpcore``, ``mcp``, and ``fastmcp`` Logger
objects (in addition to every handler on the root logger, inserted at index 0
so a handler a third party adds afterward still runs after ours), so a
record's ``msg``/``args``/``exc_info``/``stack_info`` are scrubbed before ANY
handler sees it -- including one a third party attaches directly to one of
those loggers, such as fastmcp's own non-propagating ``RichHandler``, which
never reaches the root logger's handlers at all.

What this does NOT cover: a record logged through a logger name outside that
list, whose handler is attached directly to that logger and never propagates
to root. Call ``attach_redaction(name)`` for any such logger once its name is
known -- e.g. M2 calls ``attach_redaction("fastmcp")`` again right after
importing fastmcp, so a handler fastmcp adds to its own logger at import time
is covered too.

Note: the logger-level filter added here is NOT what protects a record
from a *child* logger (e.g. ``fastmcp.server``) that merely propagates up
through ``fastmcp``'s non-propagating handler -- a filter attached via
``Logger.addFilter`` only runs for records that *originate* at that logger,
never for ones passing through it on their way up the tree via
``Logger.callHandlers``. What actually protects that case is that
``attach_redaction`` also adds the filter directly to each of the logger's
*handlers* (not just the logger object), and handler-level filters DO run for
every record the handler processes, including propagated ones. So the
handler-level attachment -- not the logger-level one -- is load-bearing for
child-logger propagation; see ``attach_redaction``'s implementation.
"""

from __future__ import annotations

import logging
import os
import sys
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mcp_atlassian_better.model import AppConfig
from mcp_atlassian_better.secrets import RedactingFilter, RedactingFormatter, Secret

LOGGER_NAME = "mcp_atlassian_better"

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_BYTES = 5_000_000
_BACKUP_COUNT = 5
_FILE_MODE = 0o600

# Marks a handler this module installed on the root logger, so a later
# reconfigure only tears down its own handlers and leaves anything else
# (e.g. pytest's caplog handler) alone.
_OWNED_ATTR = "_mcp_atlassian_better_owned"

# Same idea for a RedactingFilter this module attached directly to a Logger
# object or one of its handlers, so re-running configure_logging (new
# secrets) swaps to the new filter instead of stacking an extra one.
_FILTER_OWNED_ATTR = "_mcp_atlassian_better_owned_filter"

# Loggers a redaction filter is attached to directly (not just via root's
# handlers) because a future milestone's traffic runs through them and one
# (fastmcp) is known to attach a non-propagating handler of its own.
_REDACTED_LOGGER_NAMES: tuple[str, ...] = ("mcp_atlassian_better", "httpx", "httpcore", "mcp", "fastmcp")

# The filter instance `attach_redaction` hands out, kept current by whatever
# the most recent `configure_logging` call built from the loaded secrets.
_active_filter: RedactingFilter | None = None


class _SecureRotatingFileHandler(RotatingFileHandler):
    """A RotatingFileHandler whose log file (and each rotated backup) is 0600."""

    def _open(self) -> TextIOWrapper:
        stream = super()._open()
        os.chmod(self.baseFilename, _FILE_MODE)
        return stream


def resolve_log_dir() -> Path:
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home).expanduser() if xdg_state_home else Path.home() / ".local" / "state"
    return base / "mcp-atlassian-better" / "logs"


def collect_secrets(config: AppConfig) -> list[Secret]:
    secrets: list[Secret] = []
    for site in config.sites:
        if site.api_token is not None:
            secrets.append(site.api_token)
        if site.personal_token is not None:
            secrets.append(site.personal_token)
    if config.defaults.api_token is not None:
        secrets.append(config.defaults.api_token)
    if config.defaults.personal_token is not None:
        secrets.append(config.defaults.personal_token)
    return secrets


def _remove_owned_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _OWNED_ATTR, False):
            logger.removeHandler(handler)
            handler.close()


def _remove_owned_filters(target: logging.Logger | logging.Handler) -> None:
    for flt in list(target.filters):
        if getattr(flt, _FILTER_OWNED_ATTR, False):
            target.removeFilter(flt)


def attach_redaction(logger_name: str) -> None:
    """Attaches the current redaction filter to a logger and its handlers.

    Safe to call before the named logger's real owner is even imported:
    ``logging.getLogger(name)`` returns (creating if needed) the same shared
    ``Logger`` singleton that owner will later call ``getLogger`` on too, so
    a filter added now still runs on any handler that owner adds afterward --
    including one on a logger with ``propagate = False`` (like fastmcp's),
    which never reaches the root logger's handlers at all. A no-op before
    the first ``configure_logging`` call (no filter to attach yet).
    """
    if _active_filter is None:
        return
    logger = logging.getLogger(logger_name)
    _remove_owned_filters(logger)
    logger.addFilter(_active_filter)
    for handler in logger.handlers:
        _remove_owned_filters(handler)
        handler.addFilter(_active_filter)


def configure_logging(config: AppConfig | None, *, verbose: bool = False) -> logging.Logger:
    """(Re)configures logging. Safe to call more than once (e.g. in tests)."""
    global _active_filter
    package_logger = logging.getLogger(LOGGER_NAME)
    package_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    package_logger.propagate = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)
    _remove_owned_handlers(root)

    secrets = collect_secrets(config) if config is not None else []
    formatter = RedactingFormatter(_FORMAT, secrets)
    redact_filter = RedactingFilter(secrets)
    setattr(redact_filter, _FILTER_OWNED_ATTR, True)
    _active_filter = redact_filter

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redact_filter)
    setattr(stream_handler, _OWNED_ATTR, True)
    # Inserted at index 0 (not appended) so a handler a third party adds to
    # root afterward still runs after ours, and therefore only ever sees a
    # record we've already scrubbed.
    root.handlers.insert(0, stream_handler)

    log_dir = resolve_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler: logging.Handler = _SecureRotatingFileHandler(
            str(log_dir / "server.log"), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redact_filter)
        setattr(file_handler, _OWNED_ATTR, True)
        root.handlers.insert(0, file_handler)
    except OSError:
        package_logger.warning("could not open log file under %s; file logging disabled", log_dir)

    # httpx logs the outbound request line -- including the full URL -- at
    # INFO, and httpcore's DEBUG level logs the full wire trace on top of
    # that. For the attachment download's cross-host redirect, that URL is a
    # pre-signed media-CDN URL carrying a `token=` query parameter that on
    # its own fetches the file with no further auth. Capped at WARNING
    # ALWAYS -- never raised to INFO/DEBUG under --verbose, unlike every
    # other logger this module configures -- so that token can never reach
    # server.log via either logger's own request-line/wire-trace logging.
    # `RedactingFilter` additionally strips any URL's query string
    # unconditionally (see `secrets.scrub_urls`) as defense in depth, in case
    # a future httpx/httpcore version -- or a different library entirely --
    # logs a URL at WARNING or above.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    for logger_name in _REDACTED_LOGGER_NAMES:
        attach_redaction(logger_name)

    return package_logger
