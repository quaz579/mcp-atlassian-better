"""enforce_site_policy: the projects_filter checks run BEFORE _validate_issue_key
(wrapper_tools.py), so its error messages are the raw caller-supplied
issue_key -- these tests pin that they stay bounded and safely escaped."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from mcp_atlassian_better.model import SiteConfig
from mcp_atlassian_better.secrets import Secret
from mcp_atlassian_better.site_policy import enforce_site_policy


def _filtered_site(*, projects_filter: tuple[str, ...] = ("ACME",)) -> SiteConfig:
    return SiteConfig(
        name="acme",
        url="https://acme.atlassian.net",
        key_prefixes=("ACME",),
        username="you@example.com",
        api_token=Secret("token"),
        projects_filter=projects_filter,
    )


def test_projects_filter_mismatch_bounds_a_pathological_key() -> None:
    huge_key = "OTHER-" + "9" * 5000  # a well-shaped key, but not in the site's projects_filter
    with pytest.raises(ToolError) as exc_info:
        enforce_site_policy(_filtered_site(), "jira_get_issue", is_write=False, issue_key=huge_key)
    message = str(exc_info.value)
    assert len(message) < 400
    assert "...(" in message


def test_projects_filter_mismatch_bounds_a_pathological_project() -> None:
    huge_key = "B" * 5000 + "-1"  # a well-shaped key, but its project isn't in the site's projects_filter
    with pytest.raises(ToolError) as exc_info:
        enforce_site_policy(_filtered_site(), "jira_get_issue", is_write=False, issue_key=huge_key)
    message = str(exc_info.value)
    assert len(message) < 400
    assert "...(" in message


def test_projects_filter_malformed_key_bounds_a_pathological_key() -> None:
    huge_key = "9" * 5000  # never matches ISSUE_KEY_RE -- the "malformed" branch
    with pytest.raises(ToolError) as exc_info:
        enforce_site_policy(_filtered_site(), "jira_get_issue", is_write=False, issue_key=huge_key)
    message = str(exc_info.value)
    assert len(message) < 400
    assert "...(" in message


def test_projects_filter_malformed_key_escapes_embedded_newline() -> None:
    # `candidate` is only `.strip()`-ed (trims the ends, not the middle), and
    # ISSUE_KEY_RE's trailing `$` can't match before a newline that isn't the
    # string's last character, so this lands in the "malformed" branch (like
    # test_projects_filter_malformed_key_bounds_a_pathological_key above),
    # echoing the raw issue_key with its embedded newline escaped by repr().
    key_with_newline = "OTHER-1\nX-Forwarded-For: evil"
    with pytest.raises(ToolError) as exc_info:
        enforce_site_policy(_filtered_site(), "jira_get_issue", is_write=False, issue_key=key_with_newline)
    message = str(exc_info.value)
    assert "\\n" in message
    assert "\n" not in message
