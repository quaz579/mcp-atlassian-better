"""Secret masking and log redaction: nothing sensitive reaches stdout/stderr."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from mcp_atlassian_better.cli import main
from mcp_atlassian_better.config import load_config
from mcp_atlassian_better.logging_setup import attach_redaction, configure_logging, resolve_log_dir
from mcp_atlassian_better.secrets import RedactingFilter, RedactingFormatter, Secret, redact_text, scrub_urls
from mcp_atlassian_better.sources import EnvOverlaySource, TomlFileConfigSource

TOKEN = "super-secret-token-value"


def test_secret_repr_and_str_are_masked() -> None:
    secret = Secret(TOKEN)
    assert TOKEN not in repr(secret)
    assert TOKEN not in str(secret)
    assert repr(secret) == "Secret('***')"
    assert str(secret) == "***"
    assert secret.get_secret_value() == TOKEN


def test_secret_equality_compares_underlying_value() -> None:
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")


def test_redacting_filter_scrubs_secret_from_message() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"token is {TOKEN}",
        args=(),
        exc_info=None,
    )
    filt = RedactingFilter([Secret(TOKEN)])
    assert filt.filter(record) is True
    assert TOKEN not in record.getMessage()
    assert "***" in record.getMessage()


def test_redacting_filter_ignores_short_values() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="value is ab",
        args=(),
        exc_info=None,
    )
    filt = RedactingFilter([Secret("ab")])
    filt.filter(record)
    assert record.getMessage() == "value is ab"


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
        [defaults]
        username = "you@example.com"
        api_token = "{TOKEN}"

        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        """
    )
    return path


def test_print_config_never_shows_the_real_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path)
    exit_code = main(["--print-config", "--config", str(config_path)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert "***" in captured.out
    assert "acme" in captured.out


def test_print_config_shows_the_env_var_name_for_an_env_sourced_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACME_TOKEN", TOKEN)
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [defaults]
        username = "you@example.com"

        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        api_token_env = "ACME_TOKEN"
        """
    )
    exit_code = main(["--print-config", "--config", str(path)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "env:ACME_TOKEN" in captured.out
    assert TOKEN not in captured.out


def test_redacting_formatter_scrubs_a_formatted_traceback() -> None:
    formatter = RedactingFormatter("%(message)s", [Secret(TOKEN)])
    try:
        raise RuntimeError(f"failure near token {TOKEN}")
    except RuntimeError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="x",
            args=(),
            exc_info=sys.exc_info(),
        )
    formatted = formatter.format(record)
    assert TOKEN not in formatted
    assert "***" in formatted


def test_redact_text_masks_every_known_secret() -> None:
    assert redact_text(f"a={TOKEN} b=other", [Secret(TOKEN)]) == "a=*** b=other"


def test_scrub_urls_strips_the_query_string_but_keeps_the_rest_of_the_line() -> None:
    text = 'HTTP Request: GET https://api.media.atlassian.com/file/binary?token=SUPERSECRET "HTTP/1.1 200 OK"'
    scrubbed = scrub_urls(text)
    assert "SUPERSECRET" not in scrubbed
    assert "https://api.media.atlassian.com/file/binary?***" in scrubbed
    assert "HTTP/1.1 200 OK" in scrubbed


def test_scrub_urls_is_a_no_op_on_a_url_with_no_query_string() -> None:
    assert scrub_urls("see https://acme.atlassian.net/browse/ACME-1 for details") == (
        "see https://acme.atlassian.net/browse/ACME-1 for details"
    )


def test_configure_logging_redacts_exception_text_from_stderr_and_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])
    logger = configure_logging(config, verbose=False)

    try:
        raise RuntimeError(f"secret leak attempt {TOKEN}")
    except RuntimeError:
        logger.error("something failed", exc_info=True)

    stderr = capsys.readouterr().err
    assert TOKEN not in stderr
    assert "***" in stderr

    log_file = resolve_log_dir() / "server.log"
    assert log_file.is_file()
    contents = log_file.read_text()
    assert TOKEN not in contents
    assert "***" in contents


def test_configure_logging_installs_redacting_filter_on_every_handler(tmp_path: Path) -> None:
    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])
    configure_logging(config, verbose=False)
    root = logging.getLogger()
    owned_handlers = [h for h in root.handlers if getattr(h, "_mcp_atlassian_better_owned", False)]
    assert owned_handlers
    for handler in owned_handlers:
        assert any(isinstance(f, RedactingFilter) for f in handler.filters)
        assert isinstance(handler.formatter, RedactingFormatter)


def test_log_file_is_created_with_mode_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])
    configure_logging(config, verbose=False)
    log_file = resolve_log_dir() / "server.log"
    assert log_file.is_file()
    assert (log_file.stat().st_mode & 0o777) == 0o600


def test_print_config_shows_the_dc_auth_branch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
        [[sites]]
        name = "onprem"
        url = "https://jira.example.com"
        key_prefixes = ["ONPREM"]
        personal_token = "{TOKEN}"
        """
    )
    exit_code = main(["--print-config", "--config", str(path)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "server_dc" in captured.out
    assert TOKEN not in captured.out


def test_print_config_labels_a_literal_token_from_the_env_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        """
    )
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_SITE_ACME_USERNAME", "you@example.com")
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_SITE_ACME_API_TOKEN", TOKEN)

    exit_code = main(["--print-config", "--config", str(path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "env overlay" in captured.out
    assert TOKEN not in captured.out


def test_redacting_filter_does_not_crash_on_a_malformed_log_call() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="bad %d",
        args=("x",),
        exc_info=None,
    )
    filt = RedactingFilter([Secret(TOKEN)])

    assert filt.filter(record) is True
    assert "[log-format-error]" in record.getMessage()
    assert record.args == ()


def test_malformed_log_call_through_configured_logging_does_not_raise_and_is_marked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Primed first (as cli.main does) so an earlier test's now-closed capsys
    # stream can't still be attached to root when load_config's own advisory
    # logging runs, which would otherwise print unrelated "Logging error"
    # noise this test isn't about.
    configure_logging(None, verbose=False)
    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])
    logger = configure_logging(config, verbose=False)

    logger.error("bad %d", "x")  # must not raise, and must not fall into handleError

    stderr = capsys.readouterr().err
    assert "[log-format-error]" in stderr
    assert "Arguments:" not in stderr


def test_chained_exception_and_stack_info_are_still_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])
    logger = configure_logging(config, verbose=False)

    try:
        try:
            raise ValueError(f"inner leak {TOKEN}")
        except ValueError as inner:
            raise RuntimeError(f"outer leak {TOKEN}") from inner
    except RuntimeError:
        logger.error("chained failure", exc_info=True, stack_info=True)

    stderr = capsys.readouterr().err
    assert TOKEN not in stderr
    assert "***" in stderr


def test_late_handler_on_a_redacted_logger_still_gets_a_redacted_traceback(tmp_path: Path) -> None:
    """The r2 reproduction: a handler attached AFTER configure_logging (no
    RedactingFormatter/Filter of its own -- standing in for fastmcp's own
    RichHandler on the non-propagating "fastmcp" logger) must still see a
    redacted exc_info, because the logger-level filter runs first."""
    import io

    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])
    configure_logging(config, verbose=False)

    stream = io.StringIO()
    late_handler = logging.StreamHandler(stream)
    late_handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("fastmcp")
    logger.addHandler(late_handler)
    logger.propagate = False
    try:
        try:
            raise RuntimeError(f"late leak {TOKEN}")
        except RuntimeError:
            logger.error("late handler test", exc_info=True)
    finally:
        logger.removeHandler(late_handler)
        logger.propagate = True

    output = stream.getvalue()
    assert TOKEN not in output
    assert "***" in output


def test_child_logger_record_redacted_via_parents_non_propagating_handler(tmp_path: Path) -> None:
    """M2's server.py calls ``attach_redaction("fastmcp")`` again right after
    importing fastmcp, because fastmcp attaches its own non-propagating
    handler directly to the "fastmcp" logger. A record from a CHILD logger
    (e.g. "fastmcp.server", standing in for fastmcp's real per-module
    loggers) still reaches that handler via normal propagation up the
    logger tree -- it must come out redacted too."""
    import io

    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])
    configure_logging(config, verbose=False)

    stream = io.StringIO()
    parent_logger = logging.getLogger("fastmcp")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    parent_logger.addHandler(handler)
    parent_logger.propagate = False
    attach_redaction("fastmcp")

    child_logger = logging.getLogger("fastmcp.server")
    try:
        child_logger.error(f"child leak {TOKEN}")
    finally:
        parent_logger.removeHandler(handler)
        parent_logger.propagate = True

    output = stream.getvalue()
    assert TOKEN not in output
    assert "***" in output


def test_httpx_and_httpcore_stay_at_warning_even_when_verbose(tmp_path: Path) -> None:
    """httpx logs each outbound request's full URL at INFO, and httpcore's
    DEBUG level logs the full wire trace -- for the attachment download's
    cross-host redirect that's a pre-signed CDN URL carrying a `token=` query
    parameter. `--verbose` must not raise these two loggers above WARNING
    even though it does exactly that for everything else, or that token
    reaches server.log."""
    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])

    configure_logging(config, verbose=True)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("mcp_atlassian_better").level == logging.DEBUG


def test_a_url_with_a_token_query_param_never_reaches_stderr_or_the_log_file_even_under_verbose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The r2 auditor's exact reproduction: a pre-signed media-CDN URL logged
    by httpx (or any other redacted logger) at a level `--verbose` doesn't
    even reach (httpx/httpcore are capped at WARNING now, not raised to INFO
    like everything else) must still come out scrubbed -- both because the
    cap holds, and because `RedactingFilter` strips any URL's query string
    unconditionally as a second, independent layer."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = load_config(sources=[TomlFileConfigSource(_write_config(tmp_path)), EnvOverlaySource({})])
    configure_logging(config, verbose=True)

    httpx_logger = logging.getLogger("httpx")
    httpx_logger.info(
        'HTTP Request: GET https://api.media.atlassian.com/file/binary?token=SUPERSECRET123 "HTTP/1.1 200 OK"'
    )
    # httpx is capped at WARNING, so the INFO call above produces no output
    # at all -- log the same line at WARNING too, proving the second,
    # independent layer (RedactingFilter's URL scrubbing) also holds even for
    # a level verbose users DO see.
    httpx_logger.warning(
        'HTTP Request: GET https://api.media.atlassian.com/file/binary?token=SUPERSECRET123 "HTTP/1.1 200 OK"'
    )

    stderr = capsys.readouterr().err
    assert "SUPERSECRET123" not in stderr
    assert "***" in stderr

    log_file = resolve_log_dir() / "server.log"
    contents = log_file.read_text()
    assert "SUPERSECRET123" not in contents
    assert "***" in contents
