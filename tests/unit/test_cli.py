import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_atlassian_better import __version__
from mcp_atlassian_better.cli import main


def test_version_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0


def test_help_and_version_require_no_config_file() -> None:
    # Must work on a clean machine/CI runner with no config.toml anywhere.
    with pytest.raises(SystemExit):
        main(["--help"])
    with pytest.raises(SystemExit):
        main(["--version"])


def test_missing_config_exits_2_with_no_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope.toml"
    exit_code = main(["--check", "--config", str(missing)])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err


def test_warm_inserts_refresh_right_after_uvx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [defaults]
        username = "you@example.com"
        api_token = "token"

        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        """
    )
    captured_command: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = main(["--warm", "--refresh", "--config", str(config_path)])

    assert exit_code == 0
    assert captured_command == ["uvx", "--refresh", "mcp-atlassian@latest", "--help"]


def test_warm_without_refresh_does_not_insert_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        personal_token = "token"
        """
    )
    captured_command: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = main(["--warm", "--config", str(config_path)])

    assert exit_code == 0
    assert captured_command == ["uvx", "mcp-atlassian@latest", "--help"]


def test_warm_refresh_inserts_after_uv_tool_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [upstream]
        command = ["uv", "tool", "run", "mcp-atlassian"]

        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        personal_token = "token"
        """
    )
    captured_command: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = main(["--warm", "--refresh", "--config", str(config_path)])

    assert exit_code == 0
    assert captured_command == ["uv", "tool", "run", "--refresh", "mcp-atlassian", "--help"]


def test_warm_refresh_on_non_uvx_command_errors_clearly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [upstream]
        command = ["python", "-m", "mcp_atlassian"]

        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        personal_token = "token"
        """
    )

    exit_code = main(["--warm", "--refresh", "--config", str(config_path)])

    assert exit_code == 1
    assert "--refresh requires" in capsys.readouterr().err


def test_warm_uses_a_minimal_explicit_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_API_TOKEN", "should-not-leak-into-the-child")
    monkeypatch.setenv("SOME_OTHER_SECRET_LOOKING_VAR", "also-should-not-leak")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [defaults]
        call_timeout_seconds = 45

        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        personal_token = "token"
        """
    )
    captured_env: dict[str, str] = {}
    captured_timeout: list[float] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_env.update(kwargs.get("env") or {})
        captured_timeout.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = main(["--warm", "--config", str(config_path)])

    assert exit_code == 0
    assert "PATH" in captured_env
    assert "JIRA_API_TOKEN" not in captured_env
    assert "SOME_OTHER_SECRET_LOOKING_VAR" not in captured_env
    assert captured_timeout == [45.0]


def test_warm_stdin_is_devnull(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An upstream that doesn't recognize the trailing `--help` could start
    serving MCP on inherited stdin -- the live pipe this process itself
    would otherwise use to talk to a client -- and eat a real request. Also
    proved end to end with a real subprocess in the adversarial-loop real
    run (pytest's own default stdin redirection makes an in-process
    behavioral assertion here unreliable)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        personal_token = "token"
        """
    )
    captured_stdin: list[object] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_stdin.append(kwargs.get("stdin"))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = main(["--warm", "--config", str(config_path)])

    assert exit_code == 0
    assert captured_stdin == [subprocess.DEVNULL]


def test_warm_timeout_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        personal_token = "token"
        """
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=command, timeout=120)

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = main(["--warm", "--config", str(config_path)])

    assert exit_code == 1
