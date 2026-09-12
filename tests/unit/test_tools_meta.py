"""Sanity checks on the tool-name constants routing and config validation rely on."""

from __future__ import annotations

from mcp_atlassian_better.tools_meta import (
    CURATED_TOOLS,
    ISSUE_KEY_ARGS,
    ISSUE_KEY_RE,
    NEVER_PARSED,
    PROJECT_KEY_ARGS,
    PROJECT_KEY_RE,
    PROJECTS_FILTER_ARGS,
    WRAPPER_OWNED_TOOLS,
    shorten_for_error,
)


def test_curated_tools_are_all_jira_prefixed() -> None:
    assert all(name.startswith("jira_") for name in CURATED_TOOLS)


_WRAPPER_ONLY_TOOLS = frozenset(
    {"jira_sites", "jira_list_attachments", "jira_upload_attachments", "jira_delete_comment"}
)


def test_wrapper_owned_tools_shadowing_an_upstream_tool_are_curated() -> None:
    # jira_sites/jira_list_attachments/jira_upload_attachments/jira_delete_comment
    # are wrapper-only and were never upstream tool names, so they aren't (and
    # shouldn't be) in CURATED_TOOLS; every WRAPPER_OWNED_TOOLS entry that DOES
    # shadow a real upstream tool (currently just jira_download_attachments)
    # must still be curated.
    assert (WRAPPER_OWNED_TOOLS - _WRAPPER_ONLY_TOOLS) <= CURATED_TOOLS
    assert _WRAPPER_ONLY_TOOLS.isdisjoint(CURATED_TOOLS)


def test_jql_is_never_a_routing_argument() -> None:
    assert "jql" not in ISSUE_KEY_ARGS
    assert "jql" not in PROJECT_KEY_ARGS
    assert "jql" not in PROJECTS_FILTER_ARGS
    assert NEVER_PARSED == frozenset({"jql"})


def test_never_parsed_does_not_overlap_any_routable_argument() -> None:
    routable = frozenset(ISSUE_KEY_ARGS) | frozenset(PROJECT_KEY_ARGS) | frozenset(PROJECTS_FILTER_ARGS)
    assert not (NEVER_PARSED & routable)


def test_projects_filter_is_a_routable_argument() -> None:
    assert PROJECTS_FILTER_ARGS == ("projects_filter",)


def test_issue_key_regex_examples() -> None:
    assert ISSUE_KEY_RE.match("ACME-123")
    assert ISSUE_KEY_RE.match("ACME-123-4")
    assert not ISSUE_KEY_RE.match("acme-123")
    assert not ISSUE_KEY_RE.match("123-ACME")


def test_project_key_regex_examples() -> None:
    assert PROJECT_KEY_RE.match("ACME")
    assert PROJECT_KEY_RE.match("ACME_OPS")
    assert not PROJECT_KEY_RE.match("10001")
    assert not PROJECT_KEY_RE.match("acme")


def test_project_key_regex_allows_a_single_letter_like_upstream() -> None:
    assert PROJECT_KEY_RE.match("X")


def test_issue_key_regex_allows_a_single_letter_project_prefix() -> None:
    match = ISSUE_KEY_RE.match("X-1")
    assert match is not None
    assert match.group(1) == "X"


def test_shorten_for_error_at_limit_has_no_suffix() -> None:
    value = "x" * 80
    assert shorten_for_error(value, limit=80) == repr(value)


def test_shorten_for_error_over_limit_adds_suffix_with_original_length() -> None:
    value = "x" * 81
    result = shorten_for_error(value, limit=80)
    assert result == repr(f"{value[:80]}...(81 chars)")
    assert "81 chars" in result


def test_shorten_for_error_escapes_a_newline() -> None:
    assert "\\n" in shorten_for_error("a\nb", limit=80)


def test_shorten_for_error_does_not_raise_on_an_astral_character() -> None:
    shorten_for_error("\U0001f600" * 200, limit=80)


def test_shorten_for_error_does_not_raise_on_a_lone_surrogate() -> None:
    shorten_for_error("\ud800" + "x" * 200, limit=80)


def test_issue_key_regex_rejects_non_ascii_digits() -> None:
    # Python's `\d` also matches non-ASCII decimal digits (Arabic-Indic,
    # fullwidth, N'Ko, ...); ISSUE_KEY_RE uses `[0-9]` specifically to
    # exclude them.
    assert not ISSUE_KEY_RE.fullmatch("CAP-١")
    assert not ISSUE_KEY_RE.fullmatch("CAP-１")
    assert not ISSUE_KEY_RE.fullmatch("CAP-߁")
