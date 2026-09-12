"""Site resolution: every routing edge case from the design doc's algorithm."""

from __future__ import annotations

import pytest

from mcp_atlassian_better.errors import (
    AmbiguousSiteError,
    CrossSiteError,
    UnknownPrefixError,
    UnknownSiteError,
)
from mcp_atlassian_better.model import SiteConfig
from mcp_atlassian_better.registry import SiteRegistry, resolve_site
from mcp_atlassian_better.secrets import Secret


def _site(name: str, *prefixes: str) -> SiteConfig:
    return SiteConfig(
        name=name,
        url=f"https://{name}.atlassian.net",
        key_prefixes=prefixes,
        username="you@example.com",
        api_token=Secret("token"),
    )


@pytest.fixture
def multi_site_registry() -> SiteRegistry:
    return SiteRegistry(
        [
            _site("acme", "ACME", "ACMEOPS"),
            _site("beta", "BETA"),
            _site("jm", "JM"),
            _site("jmz", "JMZ"),
        ]
    )


@pytest.fixture
def single_site_registry() -> SiteRegistry:
    return SiteRegistry([_site("acme", "ACME")])


def test_lowercase_key_still_resolves(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"issue_key": "acme-123"}, tool_name="jira_get_issue")
    assert result.site.name == "acme"
    assert "ACME-123" in result.reason


def test_comma_separated_string(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(
        multi_site_registry, {"issue_keys": "ACME-1,ACME-2"}, tool_name="jira_batch_get_changelogs"
    )
    assert result.site.name == "acme"


def test_list_of_keys(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"issue_ids_or_keys": ["ACME-1", "ACME-2"]}, tool_name="x")
    assert result.site.name == "acme"


def test_multi_segment_issue_key(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"issue_key": "ACME-123-4"}, tool_name="x")
    assert result.site.name == "acme"


def test_prefix_exact_match_not_startswith(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"issue_key": "JM-1"}, tool_name="x")
    assert result.site.name == "jm"
    result = resolve_site(multi_site_registry, {"issue_key": "JMZ-1"}, tool_name="x")
    assert result.site.name == "jmz"


def test_empty_and_none_values_are_skipped(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(AmbiguousSiteError):
        resolve_site(multi_site_registry, {"issue_key": None, "epic_key": "", "parent": "  "}, tool_name="x")


def test_jql_is_never_parsed(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(AmbiguousSiteError):
        resolve_site(multi_site_registry, {"jql": "project = ACME"}, tool_name="jira_search")


def test_url_argument_is_never_parsed(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(AmbiguousSiteError):
        resolve_site(multi_site_registry, {"url": "https://acme.atlassian.net/browse/ACME-1"}, tool_name="x")


def test_project_key_and_contradicting_issue_key_is_cross_site(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(CrossSiteError) as exc_info:
        resolve_site(multi_site_registry, {"project_key": "BETA", "issue_key": "ACME-1"}, tool_name="x")
    message = str(exc_info.value)
    assert "acme" in message
    assert "beta" in message


def test_explicit_wins_over_contradicting_key(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"issue_key": "ACME-1"}, explicit="beta", tool_name="x")
    assert result.site.name == "beta"
    assert result.reason == "explicit"


def test_explicit_is_case_insensitive(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {}, explicit="ACME", tool_name="x")
    assert result.site.name == "acme"


def test_explicit_is_stripped_of_surrounding_whitespace(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {}, explicit="  acme  ", tool_name="x")
    assert result.site.name == "acme"
    assert result.reason == "explicit"


def test_explicit_all_whitespace_falls_through_to_key_inference(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"issue_key": "BETA-1"}, explicit="   ", tool_name="x")
    assert result.site.name == "beta"


def test_unknown_explicit_site_lists_configured_names(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(UnknownSiteError) as exc_info:
        resolve_site(multi_site_registry, {}, explicit="nope", tool_name="x")
    message = str(exc_info.value)
    assert "acme" in message and "beta" in message


def test_unknown_explicit_site_bounds_a_pathological_name(multi_site_registry: SiteRegistry) -> None:
    huge_name = "x" * 5000
    with pytest.raises(UnknownSiteError) as exc_info:
        resolve_site(multi_site_registry, {}, explicit=huge_name, tool_name="x")
    message = str(exc_info.value)
    assert len(message) < 400
    assert "...(" in message


def test_single_site_shortcut_ignores_arguments(single_site_registry: SiteRegistry) -> None:
    result = resolve_site(single_site_registry, {"issue_key": "does-not-matter"}, tool_name="x")
    assert result.site.name == "acme"
    assert result.reason == "only configured site"


def test_unknown_prefix_names_it(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(UnknownPrefixError, match="ZZZ"):
        resolve_site(multi_site_registry, {"issue_key": "ZZZ-1"}, tool_name="x")


def test_unknown_prefix_bounds_a_pathological_key(multi_site_registry: SiteRegistry) -> None:
    # ISSUE_KEY_RE has no upper bound on the digit run, so an unknown-prefix
    # key this long reached resolve_site's error message untruncated before
    # every pre-validation echo site shared tools_meta.shorten_for_error.
    huge_key = "ZZZ-" + "9" * 5000
    with pytest.raises(UnknownPrefixError) as exc_info:
        resolve_site(multi_site_registry, {"issue_key": huge_key}, tool_name="x")
    message = str(exc_info.value)
    assert len(message) < 400
    assert "...(" in message


def test_cross_site_bounds_a_pathological_key(multi_site_registry: SiteRegistry) -> None:
    # Each matched token is echoed into both `matched_detail` (CrossSiteError's
    # message) and `first_reason` (SiteResolution.reason) before any length
    # check runs, so a huge but well-shaped key on either side of the
    # cross-site conflict must be bounded by shorten_for_error too.
    huge_key = "ACME-" + "9" * 5000
    with pytest.raises(CrossSiteError) as exc_info:
        resolve_site(
            multi_site_registry, {"issue_key": huge_key, "epic_key": "BETA-1"}, tool_name="jira_get_issue"
        )
    message = str(exc_info.value)
    assert len(message) < 600
    assert "...(" in message


def test_cross_site_names_both_keys_and_sites(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(CrossSiteError) as exc_info:
        resolve_site(
            multi_site_registry, {"issue_key": "ACME-1", "epic_key": "BETA-2"}, tool_name="jira_get_issue"
        )
    message = str(exc_info.value)
    assert "ACME-1" in message
    assert "BETA-2" in message
    assert "acme" in message
    assert "beta" in message


def test_zero_keys_is_ambiguous_with_prefix_table(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(AmbiguousSiteError) as exc_info:
        resolve_site(multi_site_registry, {}, tool_name="jira_search")
    message = str(exc_info.value)
    assert "acme: ACME, ACMEOPS" in message
    assert "beta: BETA" in message


def test_prefix_table_format(multi_site_registry: SiteRegistry) -> None:
    assert multi_site_registry.prefix_table() == "acme: ACME, ACMEOPS | beta: BETA | jm: JM | jmz: JMZ"


def test_project_key_with_issue_number_suffix_is_stripped(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"project_key": "ACME-1"}, tool_name="x")
    assert result.site.name == "acme"


def test_projects_filter_routes_by_matching_project_key(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"projects_filter": "ACME"}, tool_name="jira_search")
    assert result.site.name == "acme"


def test_projects_filter_is_comma_separated(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"projects_filter": "ACME,ACMEOPS"}, tool_name="jira_search")
    assert result.site.name == "acme"


def test_projects_filter_numeric_project_id_is_skipped_not_unknown(multi_site_registry: SiteRegistry) -> None:
    with pytest.raises(AmbiguousSiteError):
        resolve_site(multi_site_registry, {"projects_filter": "10001"}, tool_name="jira_search")


def test_projects_filter_numeric_only_gives_a_present_but_unrouted_message(
    multi_site_registry: SiteRegistry,
) -> None:
    with pytest.raises(AmbiguousSiteError) as exc_info:
        resolve_site(multi_site_registry, {"projects_filter": "10001"}, tool_name="jira_search")
    message = str(exc_info.value)
    assert "projects_filter" in message
    assert "present but contained no recognizable" in message


def test_zero_keys_message_does_not_claim_an_arg_was_present_when_none_was(
    multi_site_registry: SiteRegistry,
) -> None:
    with pytest.raises(AmbiguousSiteError) as exc_info:
        resolve_site(multi_site_registry, {}, tool_name="jira_search")
    assert "present but contained no recognizable" not in str(exc_info.value)


def test_projects_filter_mixed_numeric_and_real_key_still_routes(multi_site_registry: SiteRegistry) -> None:
    result = resolve_site(multi_site_registry, {"projects_filter": "10001,BETA"}, tool_name="jira_search")
    assert result.site.name == "beta"
