"""Keeps the docs honest against the actual tool-name constants: every
`jira_*` name backticked in README.md/SKILL.md must be a real tool, every
wrapper-owned tool must be mentioned in README.md, SKILL.md must have valid
front matter, and config.example.toml must actually load."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_atlassian_better.config import load_config
from mcp_atlassian_better.sources import EnvOverlaySource, TomlFileConfigSource
from mcp_atlassian_better.tools_meta import CURATED_TOOLS, WRAPPER_OWNED_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
SKILL_MD = REPO_ROOT / "claude" / "skills" / "mcp-atlassian-better" / "SKILL.md"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.toml"

_BACKTICKED_JIRA_NAME_RE = re.compile(r"`(jira_[a-zA-Z0-9_]+)")


def _backticked_jira_names(text: str) -> set[str]:
    return set(_BACKTICKED_JIRA_NAME_RE.findall(text))


def _parse_front_matter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", "SKILL.md must open with a '---' front-matter delimiter"
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    front_matter: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        assert sep, f"SKILL.md front matter line is not 'key: value': {line!r}"
        front_matter[key.strip()] = value.strip()
    return front_matter


def test_skill_md_exists_with_valid_front_matter() -> None:
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"
    front_matter = _parse_front_matter(SKILL_MD.read_text())
    assert front_matter.get("name") == "mcp-atlassian-better"
    assert front_matter.get("description"), "SKILL.md front matter must set a non-empty 'description'"


@pytest.mark.parametrize("path", [README, SKILL_MD], ids=["README.md", "SKILL.md"])
def test_every_backticked_tool_name_is_known(path: Path) -> None:
    names = _backticked_jira_names(path.read_text())
    unknown = names - CURATED_TOOLS - WRAPPER_OWNED_TOOLS
    assert not unknown, f"{path.name} references unknown tool name(s): {sorted(unknown)}"


def test_readme_mentions_every_wrapper_owned_tool() -> None:
    text = README.read_text()
    missing = {name for name in WRAPPER_OWNED_TOOLS if name not in text}
    assert not missing, f"README.md never mentions wrapper-owned tool(s): {sorted(missing)}"


def test_config_example_toml_parses_and_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    assert CONFIG_EXAMPLE.is_file()
    # config.example.toml references JIRA_API_TOKEN via api_token_env; supply
    # a placeholder so load_config can resolve it without a real credential.
    monkeypatch.setenv("JIRA_API_TOKEN", "placeholder-token-for-test-docs")
    config = load_config(sources=[TomlFileConfigSource(CONFIG_EXAMPLE), EnvOverlaySource({})])
    assert {site.name for site in config.sites} == {"acme", "beta", "gamma"}
    assert all(site.api_token is not None for site in config.sites)
    # Both commented-out recovery keys must be real Defaults fields on the
    # loader currently on main -- catches config.example.toml drifting from
    # the actual schema after a merge (M4a introduced both).
    assert config.defaults.recovery_cooldown_seconds == 30.0
    assert config.defaults.health_recovery_budget_seconds == 8.0


def test_readme_documents_recovery_settings() -> None:
    text = README.read_text()
    assert "recovery_cooldown_seconds" in text
    assert "health_recovery_budget_seconds" in text
