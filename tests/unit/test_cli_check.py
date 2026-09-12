"""`--check` against respx-mocked Jira /myself endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response

from mcp_atlassian_better.cli import main

TOKEN = "super-secret-check-token"

CONFIG = f"""
[defaults]
username = "you@example.com"
api_token = "{TOKEN}"

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]

[[sites]]
name = "beta"
url = "https://beta.atlassian.net"
key_prefixes = ["BETA"]
"""


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


@respx.mock
def test_check_all_sites_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(200, json={"displayName": "Example User", "accountId": "acc-acme"})
    )
    respx.get("https://beta.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(200, json={"displayName": "Example User", "accountId": "acc-beta"})
    )

    config_path = _write_config(tmp_path)
    exit_code = main(["--check", "--config", str(config_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "acme" in captured.out
    assert "beta" in captured.out
    assert "Example User" in captured.out
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    # Regression: the SOURCE column and each site's resolved provenance
    # (both sites here come from the same config file) must render.
    assert "SOURCE" in captured.out
    acme_line = next(line for line in captured.out.splitlines() if line.startswith("acme"))
    assert str(config_path) in acme_line


@respx.mock
def test_check_one_site_401_fails_without_leaking_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(200, json={"displayName": "Example User", "accountId": "acc-acme"})
    )
    respx.get("https://beta.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(401, json={"errorMessages": ["Unauthorized"]})
    )

    exit_code = main(["--check", "--config", str(_write_config(tmp_path))])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "401" in captured.out
    assert "Unauthorized" in captured.out
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


@respx.mock
def test_check_allow_partial_exits_zero_when_at_least_one_site_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(200, json={"displayName": "Example User", "accountId": "acc-acme"})
    )
    respx.get("https://beta.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(401, json={"errorMessages": ["Unauthorized"]})
    )

    exit_code = main(["--check", "--allow-partial", "--config", str(_write_config(tmp_path))])

    assert exit_code == 0


@respx.mock
def test_check_allow_partial_still_fails_when_every_site_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/myself").mock(return_value=Response(401, json={}))
    respx.get("https://beta.atlassian.net/rest/api/3/myself").mock(return_value=Response(401, json={}))

    exit_code = main(["--check", "--allow-partial", "--config", str(_write_config(tmp_path))])

    assert exit_code == 1


DC_CONFIG = f"""
[[sites]]
name = "onprem"
url = "https://jira.example.com"
key_prefixes = ["ONPREM"]
personal_token = "{TOKEN}"
"""


@respx.mock
def test_check_dc_site_uses_bearer_and_v2_myself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.get("https://jira.example.com/rest/api/2/myself").mock(
        return_value=Response(200, json={"displayName": "Example User", "key": "example.user"})
    )
    path = tmp_path / "config.toml"
    path.write_text(DC_CONFIG)

    exit_code = main(["--check", "--config", str(path)])

    assert exit_code == 0
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    captured = capsys.readouterr()
    assert "bearer" in captured.out
    assert "example.user" in captured.out
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


@respx.mock
def test_check_non_http_exception_becomes_a_failed_row_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(200, json={"displayName": "Example User", "accountId": "acc-acme"})
    )
    respx.get("https://beta.atlassian.net/rest/api/3/myself").mock(
        side_effect=RuntimeError(f"boom near token {TOKEN}")
    )

    exit_code = main(["--check", "--config", str(_write_config(tmp_path))])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "acme" in captured.out
    assert "beta" in captured.out
    assert "RuntimeError" in captured.out
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


@respx.mock
def test_check_table_columns_are_not_truncated_for_long_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    long_account_id = "a" * 60
    respx.get("https://acme.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(200, json={"displayName": "Example User", "accountId": long_account_id})
    )
    respx.get("https://beta.atlassian.net/rest/api/3/myself").mock(
        return_value=Response(200, json={"displayName": "Example User", "accountId": "acc-beta"})
    )

    exit_code = main(["--check", "--config", str(_write_config(tmp_path))])

    assert exit_code == 0
    out = capsys.readouterr().out
    acme_line = next(line for line in out.splitlines() if line.startswith("acme"))
    assert long_account_id in acme_line
    # The PREFIXES column must still be present after the widened STATUS
    # column, not truncated away by a fixed-width format spec.
    assert acme_line.rstrip().endswith("ACME")
