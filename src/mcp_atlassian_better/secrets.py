"""Secret value wrapper and a logging filter that redacts secret values."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

_MASK = "***"
_MIN_REDACT_LEN = 4
# A plain Formatter used only to render exc_info into text ourselves, so we
# can redact it and store it in record.exc_text before any real handler runs.
_EXC_FORMATTER = logging.Formatter()

# Matches an http(s) URL's query string. A pre-signed CDN/media URL's query
# string can itself BE the credential (Jira's attachment CDN redirect target
# carries `?token=<jwt>` that fetches the file with no further auth), and
# that URL can reach a log line via a third-party library (httpx logs the
# outbound request line at INFO) that never goes through `Secret`/known
# values at all. So this runs unconditionally in `RedactingFilter`,
# independent of whatever secrets are configured -- stripping the whole
# query string is the simplest rule that can't miss a new parameter name
# (`token=`, `sig=`, `X-Amz-Signature=`, ...) a future CDN might use.
_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>]+?)\?[^\s\"'<>]*")


def scrub_urls(text: str) -> str:
    """Strips the query string off every http(s) URL found in ``text``."""
    return _URL_QUERY_RE.sub(r"\1?***", text)


class Secret:
    """Holds a sensitive string so it can't be printed or logged by accident.

    ``repr()`` and ``str()`` always show ``***``; the real value is only
    reachable via :meth:`get_secret_value`.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({_MASK!r})"

    def __str__(self) -> str:
        return _MASK

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


def _secret_values(secrets: Iterable[Secret]) -> tuple[str, ...]:
    """Longest-first so a token that is a substring of another is still fully masked."""
    return tuple(
        sorted(
            {s.get_secret_value() for s in secrets if len(s.get_secret_value()) >= _MIN_REDACT_LEN},
            key=len,
            reverse=True,
        )
    )


def _redact_with_values(text: str, values: tuple[str, ...]) -> str:
    redacted = text
    for value in values:
        if value in redacted:
            redacted = redacted.replace(value, _MASK)
    return redacted


def redact_text(text: str, secrets: Iterable[Secret]) -> str:
    return _redact_with_values(text, _secret_values(secrets))


class RedactingFilter(logging.Filter):
    """Scrubs known secret values out of a record before any handler runs it.

    Covers ``record.msg``/``record.args``, the rendered exception text
    (computed and stored in ``record.exc_text`` with ``record.exc_info``
    cleared, so a handler that renders its own traceback from ``exc_info``
    directly -- e.g. fastmcp's ``RichHandler`` -- can never reach the raw
    frames), and ``record.stack_info``. This only protects a record that
    passes through a logger or handler this filter is attached to; see
    ``logging_setup.attach_redaction`` for extending coverage to a logger
    discovered later (e.g. only after importing a third-party package).

    Also tolerant of a malformed third-party log call, e.g.
    ``logger.error("bad %d", "x")``: stdlib invokes a filter's
    ``getMessage()`` outside the handler's own ``try/except`` ->
    ``handleError`` safety net, so a bad format string would otherwise
    propagate straight out of ``Logger.handle`` and crash the process.
    """

    def __init__(self, secrets: Iterable[Secret]) -> None:
        super().__init__()
        self._values = _secret_values(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            self._mark_format_error(record)
        else:
            redacted = scrub_urls(message)
            if self._values:
                redacted = _redact_with_values(redacted, self._values)
            if redacted != message:
                record.msg = redacted
                record.args = ()

        # Everything below runs unconditionally (not gated on `self._values`):
        # URL query-string scrubbing is independent of which secret values are
        # configured, so even a deployment with zero known secrets still gets
        # it applied to args/exc_info/stack_info.
        record.args = self._redact_args(record.args)
        if record.exc_info:
            formatted = _EXC_FORMATTER.formatException(record.exc_info)
            if self._values:
                formatted = _redact_with_values(formatted, self._values)
            record.exc_text = scrub_urls(formatted)
            record.exc_info = None
        if record.stack_info:
            stack_text = str(record.stack_info)
            if self._values:
                stack_text = _redact_with_values(stack_text, self._values)
            record.stack_info = scrub_urls(stack_text)
        return True

    def _mark_format_error(self, record: logging.LogRecord) -> None:
        """Makes an unrenderable record safe to log instead of raising or falling
        into ``Handler.handleError``'s own raw ``Arguments: (...)`` dump.

        Setting ``record.args = ()`` also makes the rewritten ``record.msg``
        safe from a second ``%``-substitution attempt by the real formatter
        later, since stdlib's ``getMessage()`` only applies ``%`` when
        ``self.args`` is non-empty.
        """
        safe_args = self._redact_args(record.args)
        try:
            raw_msg = str(record.msg)
        except Exception:
            raw_msg = f"<unrenderable {record.msg.__class__.__name__}>"
        if self._values:
            raw_msg = _redact_with_values(raw_msg, self._values)
        record.msg = f"[log-format-error] {raw_msg} args={safe_args!r}"
        record.args = ()

    def _redact_args(self, args: Any) -> Any:
        if not args:
            return args
        if isinstance(args, Mapping):
            return {key: self._redact_one(value) for key, value in args.items()}
        if isinstance(args, tuple):
            return tuple(self._redact_one(value) for value in args)
        return args

    def _redact_one(self, value: Any) -> Any:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = repr(value)
            except Exception:
                return value
        redacted = scrub_urls(text)
        if self._values:
            redacted = _redact_with_values(redacted, self._values)
        return redacted if redacted != text else value


class RedactingFormatter(logging.Formatter):
    """Defense in depth: re-scrubs the fully formatted line.

    ``RedactingFilter`` already redacts ``msg``/``args``/``exc_text``/
    ``stack_info`` on the record itself before any handler runs, so this is
    normally redundant -- kept in case a handler's formatter builds its
    output from something the filter doesn't touch.
    """

    def __init__(self, fmt: str, secrets: Iterable[Secret]) -> None:
        super().__init__(fmt)
        self._values = _secret_values(secrets)

    def format(self, record: logging.LogRecord) -> str:
        formatted = scrub_urls(super().format(record))
        return _redact_with_values(formatted, self._values)
