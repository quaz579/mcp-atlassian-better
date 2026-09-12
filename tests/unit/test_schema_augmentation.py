"""augment_input_schema / augment_description: adds `site`, never touches
`required`, and refuses to clobber an upstream schema that already has one."""

from __future__ import annotations

import pytest

from mcp_atlassian_better.errors import SchemaConflictError
from mcp_atlassian_better.mirror import SITE_PARAM, augment_description, augment_input_schema
from mcp_atlassian_better.model import SiteConfig
from mcp_atlassian_better.registry import SiteRegistry
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
def registry() -> SiteRegistry:
    return SiteRegistry([_site("acme", "ACME", "ACMEOPS"), _site("beta", "BETA")])


def test_site_property_is_added_as_optional_enum(registry: SiteRegistry) -> None:
    schema = {"type": "object", "properties": {"issue_key": {"type": "string"}}, "required": ["issue_key"]}
    augmented = augment_input_schema(schema, registry)
    assert augmented["properties"][SITE_PARAM]["type"] == "string"
    assert set(augmented["properties"][SITE_PARAM]["enum"]) == {"acme", "beta"}


def test_required_is_never_touched(registry: SiteRegistry) -> None:
    schema = {"type": "object", "properties": {"issue_key": {"type": "string"}}, "required": ["issue_key"]}
    augmented = augment_input_schema(schema, registry)
    assert augmented["required"] == ["issue_key"]
    assert SITE_PARAM not in augmented["required"]


def test_schema_without_required_stays_without_one(registry: SiteRegistry) -> None:
    schema = {"type": "object", "properties": {"jql": {"type": "string"}}}
    augmented = augment_input_schema(schema, registry)
    assert "required" not in augmented


def test_conflict_raises_when_upstream_already_has_site(registry: SiteRegistry) -> None:
    schema = {"type": "object", "properties": {"site": {"type": "string"}}}
    with pytest.raises(SchemaConflictError):
        augment_input_schema(schema, registry)


def test_original_schema_is_not_mutated(registry: SiteRegistry) -> None:
    schema = {"type": "object", "properties": {"issue_key": {"type": "string"}}, "required": ["issue_key"]}
    augment_input_schema(schema, registry)
    assert "site" not in schema["properties"]


def test_defs_are_left_untouched(registry: SiteRegistry) -> None:
    schema = {
        "type": "object",
        "properties": {"issue_key": {"$ref": "#/$defs/IssueKey"}},
        "$defs": {"IssueKey": {"type": "string", "pattern": "^[A-Z]+-\\d+$"}},
    }
    augmented = augment_input_schema(schema, registry)
    assert augmented["$defs"] == schema["$defs"]


def test_description_carries_the_prefix_table(registry: SiteRegistry) -> None:
    description = augment_description("Gets a Jira issue.", registry)
    assert "Gets a Jira issue." in description
    assert "acme" in description
    assert "ACME" in description
    assert "beta" in description
    assert "BETA" in description


def test_description_handles_missing_upstream_description(registry: SiteRegistry) -> None:
    description = augment_description(None, registry)
    assert "acme" in description
