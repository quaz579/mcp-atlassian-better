"""Config loading and validation: TOML parsing, defaults, and every hard error."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_atlassian_better.cli import _sources_for
from mcp_atlassian_better.config import default_sources, load_config
from mcp_atlassian_better.errors import ConfigError
from mcp_atlassian_better.model import AppConfig
from mcp_atlassian_better.sources import DropInSitesSource, EnvOverlaySource, TomlFileConfigSource

MINIMAL_TOML = """
[defaults]
username = "you@example.com"
api_token = "file-token"

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME", "ACMEOPS"]
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path


def _load(tmp_path: Path, content: str, environ: dict[str, str] | None = None) -> AppConfig:
    path = _write(tmp_path, content)
    return load_config(sources=[TomlFileConfigSource(path), EnvOverlaySource(environ or {})])


def test_toml_round_trip(tmp_path: Path) -> None:
    config = _load(tmp_path, MINIMAL_TOML)
    assert len(config.sites) == 1
    site = config.sites[0]
    assert site.name == "acme"
    assert site.url == "https://acme.atlassian.net"
    assert site.key_prefixes == ("ACME", "ACMEOPS")
    assert site.username == "you@example.com"
    assert site.api_token is not None
    assert site.api_token.get_secret_value() == "file-token"
    assert site.auth_mode == "cloud"


def test_recovery_cooldown_seconds_defaults_to_thirty(tmp_path: Path) -> None:
    config = _load(tmp_path, MINIMAL_TOML)
    assert config.defaults.recovery_cooldown_seconds == 30.0


def test_recovery_cooldown_seconds_is_configurable(tmp_path: Path) -> None:
    content = MINIMAL_TOML.replace("[defaults]", "[defaults]\nrecovery_cooldown_seconds = 5\n")
    config = _load(tmp_path, content)
    assert config.defaults.recovery_cooldown_seconds == 5.0


def test_recovery_cooldown_seconds_must_be_positive(tmp_path: Path) -> None:
    content = MINIMAL_TOML.replace("[defaults]", "[defaults]\nrecovery_cooldown_seconds = 0\n")
    with pytest.raises(ConfigError, match="recovery_cooldown_seconds"):
        _load(tmp_path, content)


def test_url_trailing_slash_is_stripped(tmp_path: Path) -> None:
    content = MINIMAL_TOML.replace(
        'url = "https://acme.atlassian.net"', 'url = "https://acme.atlassian.net/"'
    )
    config = _load(tmp_path, content)
    assert config.sites[0].url == "https://acme.atlassian.net"


def test_defaults_inherited_when_site_has_no_token(tmp_path: Path) -> None:
    config = _load(tmp_path, MINIMAL_TOML)
    assert config.sites[0].api_token is not None
    assert config.sites[0].api_token.get_secret_value() == "file-token"


def test_site_overrides_default_token(tmp_path: Path) -> None:
    content = MINIMAL_TOML + '\napi_token = "site-token"\n'
    config = _load(tmp_path, content)
    assert config.sites[0].api_token is not None
    assert config.sites[0].api_token.get_secret_value() == "site-token"


def test_api_token_env_resolves_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = """
[defaults]
username = "you@example.com"
api_token_env = "TEST_JIRA_TOKEN"

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]
"""
    monkeypatch.setenv("TEST_JIRA_TOKEN", "env-token-value")
    config = _load(tmp_path, content)
    assert config.sites[0].api_token is not None
    assert config.sites[0].api_token.get_secret_value() == "env-token-value"
    assert config.sites[0].api_token_env == "TEST_JIRA_TOKEN"


def test_api_token_env_missing_var_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = """
[defaults]
username = "you@example.com"
api_token_env = "TEST_JIRA_TOKEN_MISSING"

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]
"""
    monkeypatch.delenv("TEST_JIRA_TOKEN_MISSING", raising=False)
    with pytest.raises(ConfigError, match="TEST_JIRA_TOKEN_MISSING"):
        _load(tmp_path, content)


def test_duplicate_prefix_across_sites_names_both_sites(tmp_path: Path) -> None:
    content = """
[defaults]
username = "you@example.com"
api_token = "file-token"

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]

[[sites]]
name = "beta"
url = "https://beta.atlassian.net"
key_prefixes = ["ACME"]
"""
    with pytest.raises(ConfigError) as exc_info:
        _load(tmp_path, content)
    message = str(exc_info.value)
    assert "acme" in message
    assert "beta" in message
    assert "ACME" in message


def test_bad_site_name_rejected(tmp_path: Path) -> None:
    content = MINIMAL_TOML.replace('name = "acme"', 'name = "ACME_UPPER"')
    with pytest.raises(ConfigError, match="ACME_UPPER"):
        _load(tmp_path, content)


def test_cloud_and_dc_auth_are_mutually_exclusive(tmp_path: Path) -> None:
    content = """
[defaults]
username = "you@example.com"

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]
api_token = "cloud-token"
personal_token = "dc-token"
"""
    with pytest.raises(ConfigError, match="not both"):
        _load(tmp_path, content)


def test_no_credentials_at_all_is_an_error(tmp_path: Path) -> None:
    content = """
[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]
"""
    with pytest.raises(ConfigError, match="no credentials"):
        _load(tmp_path, content)


def test_server_dc_site_does_not_require_username(tmp_path: Path) -> None:
    content = """
[[sites]]
name = "onprem"
url = "https://jira.example.com"
key_prefixes = ["ONPREM"]
personal_token = "dc-token"
"""
    config = _load(tmp_path, content)
    assert config.sites[0].auth_mode == "server_dc"
    assert config.sites[0].personal_token is not None
    assert config.sites[0].personal_token.get_secret_value() == "dc-token"


def test_insecure_file_mode_warns_but_still_loads(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    path.chmod(0o644)
    with caplog.at_level("WARNING"):
        config = load_config(sources=[TomlFileConfigSource(path), EnvOverlaySource({})])
    assert len(config.sites) == 1
    assert any("readable by group/other" in record.message for record in caplog.records)


def test_enabled_tools_must_be_subset_of_curated_allowlist(tmp_path: Path) -> None:
    content = MINIMAL_TOML + '\nenabled_tools = ["jira_get_issue", "jira_not_a_real_tool"]\n'
    with pytest.raises(ConfigError, match="jira_not_a_real_tool"):
        _load(tmp_path, content)


def test_enabled_tools_accepts_a_wrapper_owned_tool_name(tmp_path: Path) -> None:
    # jira_download_attachments is served by the wrapper itself, not forwarded
    # to the child, but it's still a legitimate name to list in enabled_tools
    # (finding 14): the allowlist is a validation concern, not a routing one.
    content = MINIMAL_TOML + '\nenabled_tools = ["jira_get_issue", "jira_download_attachments"]\n'
    config = _load(tmp_path, content)
    assert config.sites[0].enabled_tools == frozenset({"jira_get_issue", "jira_download_attachments"})


def test_projects_filter_is_normalized_to_uppercase(tmp_path: Path) -> None:
    content = MINIMAL_TOML + '\nprojects_filter = ["acme", "AcmeOps"]\n'
    config = _load(tmp_path, content)
    assert config.sites[0].projects_filter == ("ACME", "ACMEOPS")


def test_projects_filter_rejects_a_numeric_entry(tmp_path: Path) -> None:
    content = MINIMAL_TOML + '\nprojects_filter = ["ACME", "10001"]\n'
    with pytest.raises(ConfigError, match="10001"):
        _load(tmp_path, content)


def test_no_sites_configured_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no sites configured"):
        _load(tmp_path, '[defaults]\nusername = "x"\n')


def test_missing_config_file_is_a_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(ConfigError, match="not found"):
        load_config(sources=[TomlFileConfigSource(missing), EnvOverlaySource({})])


def test_key_prefixes_reject_lowercase(tmp_path: Path) -> None:
    content = MINIMAL_TOML.replace('key_prefixes = ["ACME", "ACMEOPS"]', 'key_prefixes = ["acme"]')
    with pytest.raises(ConfigError, match="acme"):
        _load(tmp_path, content)


def test_url_with_userinfo_is_rejected_and_never_echoed(tmp_path: Path) -> None:
    content = MINIMAL_TOML.replace(
        'url = "https://acme.atlassian.net"', 'url = "https://user:super-secret-token@acme.atlassian.net"'
    )
    with pytest.raises(ConfigError) as exc_info:
        _load(tmp_path, content)
    message = str(exc_info.value)
    assert "super-secret-token" not in message
    assert "userinfo" in message.lower() or "credentials" in message.lower()


def test_non_https_url_error_does_not_echo_the_raw_url(tmp_path: Path) -> None:
    content = MINIMAL_TOML.replace(
        'url = "https://acme.atlassian.net"', 'url = "http://user:token-value@acme.atlassian.net"'
    )
    with pytest.raises(ConfigError) as exc_info:
        _load(tmp_path, content)
    assert "token-value" not in str(exc_info.value)


def test_upstream_command_as_bare_string_is_rejected(tmp_path: Path) -> None:
    content = MINIMAL_TOML + '\n[upstream]\ncommand = "uvx"\n'
    with pytest.raises(ConfigError, match="upstream.command"):
        _load(tmp_path, content)


def test_upstream_command_empty_list_is_rejected(tmp_path: Path) -> None:
    content = MINIMAL_TOML + "\n[upstream]\ncommand = []\n"
    with pytest.raises(ConfigError, match="upstream.command"):
        _load(tmp_path, content)


def test_upstream_env_passthrough_as_bare_string_is_rejected(tmp_path: Path) -> None:
    content = MINIMAL_TOML + '\n[upstream]\nenv_passthrough = "HTTPS_PROXY"\n'
    with pytest.raises(ConfigError, match="upstream.env_passthrough"):
        _load(tmp_path, content)


def test_defaults_reject_both_cloud_and_dc_auth(tmp_path: Path) -> None:
    content = """
[defaults]
username = "you@example.com"
api_token = "cloud-token"
personal_token = "dc-token"

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]
"""
    with pytest.raises(ConfigError, match=r"\[defaults\]"):
        _load(tmp_path, content)


def test_api_token_env_set_but_empty_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = """
[defaults]
username = "you@example.com"
api_token_env = "TEST_JIRA_TOKEN_EMPTY"

[[sites]]
name = "acme"
url = "https://acme.atlassian.net"
key_prefixes = ["ACME"]
"""
    monkeypatch.setenv("TEST_JIRA_TOKEN_EMPTY", "")
    with pytest.raises(ConfigError, match="TEST_JIRA_TOKEN_EMPTY"):
        _load(tmp_path, content)


def test_stray_env_site_var_without_url_and_prefixes_is_dropped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    environ = {"MCP_ATLASSIAN_BETTER_SITE_TYPO_READ_ONLY": "true"}
    with caplog.at_level("WARNING"):
        config = load_config(sources=[TomlFileConfigSource(path), EnvOverlaySource(environ)])
    assert [s.name for s in config.sites] == ["acme"]
    assert any("typo" in record.message.lower() for record in caplog.records)


def test_env_overlay_can_still_add_a_complete_new_site(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    environ = {
        "MCP_ATLASSIAN_BETTER_SITE_BETA_URL": "https://beta.atlassian.net",
        "MCP_ATLASSIAN_BETTER_SITE_BETA_KEY_PREFIXES": "BETA",
        "MCP_ATLASSIAN_BETTER_SITE_BETA_API_TOKEN": "beta-token",
        "MCP_ATLASSIAN_BETTER_SITE_BETA_USERNAME": "you@example.com",
    }
    config = load_config(sources=[TomlFileConfigSource(path), EnvOverlaySource(environ)])
    assert {s.name for s in config.sites} == {"acme", "beta"}


def test_env_overlay_field_onto_existing_toml_site_is_unaffected(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    environ = {"MCP_ATLASSIAN_BETTER_SITE_ACME_READ_ONLY": "true"}
    config = load_config(sources=[TomlFileConfigSource(path), EnvOverlaySource(environ)])
    assert len(config.sites) == 1
    assert config.sites[0].read_only is True


# --- sites.d drop-in directory --------------------------------------------


def _write_drop_in(sites_dir: Path, filename: str, content: str) -> None:
    sites_dir.mkdir(parents=True, exist_ok=True)
    (sites_dir / filename).write_text(content)


def test_drop_in_site_is_loaded_and_config_toml_overrides_it_per_key(tmp_path: Path) -> None:
    sites_dir = tmp_path / "sites.d"
    _write_drop_in(
        sites_dir,
        "01-beta.toml",
        '[[sites]]\nname = "beta"\nurl = "https://beta.atlassian.net"\n'
        'key_prefixes = ["BETA"]\nusername = "drop-in@example.com"\n',
    )
    config_path = _write(
        tmp_path,
        MINIMAL_TOML + '\n[[sites]]\nname = "beta"\nusername = "you@example.com"\n',
    )
    config = load_config(
        sources=[
            DropInSitesSource(sites_dir),
            TomlFileConfigSource(config_path),
            EnvOverlaySource({}),
        ]
    )
    beta = next(s for s in config.sites if s.name == "beta")
    # config.toml only overrides `username`; `url`/`key_prefixes` still come
    # from the drop-in, proving the merge is per-key, not whole-entry.
    assert beta.username == "you@example.com"
    assert beta.url == "https://beta.atlassian.net"
    assert beta.key_prefixes == ("BETA",)
    assert beta.source == str(config_path)


def test_drop_in_only_site_gets_the_drop_in_file_as_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETA_TOKEN", "beta-token")
    sites_dir = tmp_path / "sites.d"
    _write_drop_in(
        sites_dir,
        "01-beta.toml",
        '[[sites]]\nname = "beta"\nurl = "https://beta.atlassian.net"\n'
        'key_prefixes = ["BETA"]\nusername = "you@example.com"\napi_token_env = "BETA_TOKEN"\n',
    )
    config_path = _write(tmp_path, MINIMAL_TOML)
    config = load_config(
        sources=[
            DropInSitesSource(sites_dir),
            TomlFileConfigSource(config_path),
            EnvOverlaySource({}),
        ]
    )
    beta = next(s for s in config.sites if s.name == "beta")
    assert beta.source == str(sites_dir / "01-beta.toml")


def test_duplicate_key_prefix_across_drop_in_and_config_toml_still_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETA_TOKEN", "beta-token")
    sites_dir = tmp_path / "sites.d"
    _write_drop_in(
        sites_dir,
        "01-beta.toml",
        '[[sites]]\nname = "beta"\nurl = "https://beta.atlassian.net"\n'
        'key_prefixes = ["ACME"]\nusername = "you@example.com"\napi_token_env = "BETA_TOKEN"\n',
    )
    config_path = _write(tmp_path, MINIMAL_TOML)
    with pytest.raises(ConfigError, match="ACME"):
        load_config(
            sources=[
                DropInSitesSource(sites_dir),
                TomlFileConfigSource(config_path),
                EnvOverlaySource({}),
            ]
        )


def test_pure_env_site_has_source_env(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_TOML)
    environ = {
        "MCP_ATLASSIAN_BETTER_SITE_BETA_URL": "https://beta.atlassian.net",
        "MCP_ATLASSIAN_BETTER_SITE_BETA_KEY_PREFIXES": "BETA",
        "MCP_ATLASSIAN_BETTER_SITE_BETA_API_TOKEN": "beta-token",
        "MCP_ATLASSIAN_BETTER_SITE_BETA_USERNAME": "you@example.com",
    }
    config = load_config(sources=[TomlFileConfigSource(path), EnvOverlaySource(environ)])
    beta = next(s for s in config.sites if s.name == "beta")
    assert beta.source == "env"
    acme = next(s for s in config.sites if s.name == "acme")
    assert acme.source == str(path)


def test_both_entry_points_honor_sites_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_config(sources=None)`` and the CLI's own ``_sources_for`` must
    build the identical source list from just a config path -- a prior version
    of this codebase had two separate places that constructed sources, and
    only one of them knew about sites.d (see config.default_sources)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(MINIMAL_TOML)
    sites_dir = tmp_path / "custom-sites-dir"
    monkeypatch.setenv("BETA_TOKEN", "beta-token")
    _write_drop_in(
        sites_dir,
        "01-beta.toml",
        '[[sites]]\nname = "beta"\nurl = "https://beta.atlassian.net"\n'
        'key_prefixes = ["BETA"]\nusername = "you@example.com"\napi_token_env = "BETA_TOKEN"\n',
    )
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_CONFIG", str(config_path))
    monkeypatch.setenv("MCP_ATLASSIAN_BETTER_SITES_DIR", str(sites_dir))

    config = load_config(sources=None)
    assert {s.name for s in config.sites} == {"acme", "beta"}

    cli_sources = _sources_for(config_path)
    assert cli_sources is not None
    drop_in_sources = [source for source in cli_sources if isinstance(source, DropInSitesSource)]
    assert len(drop_in_sources) == 1
    assert drop_in_sources[0].directory == sites_dir

    default_drop_in_sources = [
        source for source in default_sources(config_path) if isinstance(source, DropInSitesSource)
    ]
    assert default_drop_in_sources[0].directory == sites_dir
