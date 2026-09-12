"""ConfigSource implementations: TOML parsing, env overlay parsing, merge order."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_atlassian_better.errors import ConfigError
from mcp_atlassian_better.sources import (
    DropInSitesSource,
    EnvOverlaySource,
    RawConfig,
    TomlFileConfigSource,
    merge_sources,
)


def test_toml_file_source_keys_sites_by_name(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]

        [[sites]]
        name = "beta"
        url = "https://beta.atlassian.net"
        key_prefixes = ["BETA"]
        """
    )
    raw = TomlFileConfigSource(path).load()
    assert set(raw["sites"]) == {"acme", "beta"}
    assert raw["sites"]["acme"]["url"] == "https://acme.atlassian.net"


def test_toml_file_source_rejects_site_missing_name(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[sites]]\nurl = "https://acme.atlassian.net"\nkey_prefixes = ["ACME"]\n')
    with pytest.raises(ConfigError, match="name"):
        TomlFileConfigSource(path).load()


def test_toml_file_source_rejects_duplicate_name_in_same_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]

        [[sites]]
        name = "acme"
        url = "https://acme2.atlassian.net"
        key_prefixes = ["ACME2"]
        """
    )
    with pytest.raises(ConfigError, match="duplicate"):
        TomlFileConfigSource(path).load()


def test_env_overlay_parses_site_fields() -> None:
    environ = {
        "MCP_ATLASSIAN_BETTER_SITE_ACME_URL": "https://acme.atlassian.net",
        "MCP_ATLASSIAN_BETTER_SITE_ACME_KEY_PREFIXES": "ACME, ACMEOPS",
        "MCP_ATLASSIAN_BETTER_SITE_ACME_API_TOKEN_ENV": "ACME_TOKEN",
        "MCP_ATLASSIAN_BETTER_SITE_ACME_READ_ONLY": "true",
        "UNRELATED_VAR": "ignored",
    }
    raw = EnvOverlaySource(environ).load()
    assert raw["sites"]["acme"]["url"] == "https://acme.atlassian.net"
    assert raw["sites"]["acme"]["key_prefixes"] == ["ACME", "ACMEOPS"]
    assert raw["sites"]["acme"]["api_token_env"] == "ACME_TOKEN"
    assert raw["sites"]["acme"]["read_only"] is True


def test_env_overlay_handles_underscore_in_site_name() -> None:
    environ = {"MCP_ATLASSIAN_BETTER_SITE_MY_SITE_URL": "https://my-site.atlassian.net"}
    raw = EnvOverlaySource(environ).load()
    assert raw["sites"]["my_site"]["url"] == "https://my-site.atlassian.net"


def test_env_overlay_rejects_unparseable_bool() -> None:
    environ = {"MCP_ATLASSIAN_BETTER_SITE_ACME_READ_ONLY": "maybe"}
    with pytest.raises(ConfigError, match="MCP_ATLASSIAN_BETTER_SITE_ACME_READ_ONLY"):
        EnvOverlaySource(environ).load()


def test_merge_sources_later_wins_field_by_field() -> None:
    first: RawConfig = {
        "defaults": {"username": "a"},
        "upstream": {},
        "sites": {"acme": {"url": "https://acme.atlassian.net", "key_prefixes": ["ACME"]}},
    }
    second: RawConfig = {
        "defaults": {"toolset_preset": "all"},
        "upstream": {},
        "sites": {"acme": {"key_prefixes": ["ACME", "ACMEOPS"]}},
    }
    merged = merge_sources(first, second)
    assert merged["defaults"] == {"username": "a", "toolset_preset": "all"}
    assert merged["sites"]["acme"]["url"] == "https://acme.atlassian.net"
    assert merged["sites"]["acme"]["key_prefixes"] == ["ACME", "ACMEOPS"]


def test_toml_file_source_invalid_utf8_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b'[defaults]\nusername = "\xff\xfe not valid utf-8"\n')
    with pytest.raises(ConfigError, match="UTF-8"):
        TomlFileConfigSource(path).load()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_toml_file_source_unreadable_file_is_a_config_error_not_a_raw_oserror(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[sites]]\nname = "acme"\n')
    path.chmod(0o000)
    try:
        with pytest.raises(ConfigError, match="could not read config file"):
            TomlFileConfigSource(path).load()
    finally:
        path.chmod(0o600)


def test_merge_sources_can_add_a_new_site_without_touching_others() -> None:
    first: RawConfig = {
        "defaults": {},
        "upstream": {},
        "sites": {"acme": {"url": "https://acme.atlassian.net"}},
    }
    second: RawConfig = {
        "defaults": {},
        "upstream": {},
        "sites": {"beta": {"url": "https://beta.atlassian.net"}},
    }
    merged = merge_sources(first, second)
    assert set(merged["sites"]) == {"acme", "beta"}


def test_toml_file_source_records_provenance(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[sites]]\nname = "acme"\nurl = "https://acme.atlassian.net"\n')
    raw = TomlFileConfigSource(path).load()
    assert raw["sources"] == {"acme": str(path)}


def test_merge_sources_merges_provenance_last_wins() -> None:
    first: RawConfig = {"defaults": {}, "upstream": {}, "sites": {}, "sources": {"acme": "first"}}
    second: RawConfig = {"defaults": {}, "upstream": {}, "sites": {}, "sources": {"acme": "second"}}
    merged = merge_sources(first, second)
    assert merged["sources"] == {"acme": "second"}


# --- DropInSitesSource ---------------------------------------------------


def test_drop_in_source_missing_directory_is_empty(tmp_path: Path) -> None:
    raw = DropInSitesSource(tmp_path / "does-not-exist").load()
    assert raw["sites"] == {}
    assert raw["sources"] == {}


def test_drop_in_source_ignores_non_toml_files(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not toml")
    raw = DropInSitesSource(tmp_path).load()
    assert raw["sites"] == {}


def test_drop_in_source_loads_sites_in_lexical_order_last_wins_per_key(tmp_path: Path) -> None:
    (tmp_path / "01-acme.toml").write_text(
        '[[sites]]\nname = "acme"\nurl = "https://acme.atlassian.net"\nkey_prefixes = ["ACME"]\n'
    )
    (tmp_path / "02-acme-override.toml").write_text('[[sites]]\nname = "acme"\nread_only = true\n')
    raw = DropInSitesSource(tmp_path).load()
    assert raw["sites"]["acme"]["url"] == "https://acme.atlassian.net"
    assert raw["sites"]["acme"]["key_prefixes"] == ["ACME"]
    assert raw["sites"]["acme"]["read_only"] is True
    assert raw["sources"]["acme"] == str(tmp_path / "02-acme-override.toml")


def test_drop_in_source_rejects_non_sites_top_level_table(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('[defaults]\nusername = "you@example.com"\n')
    with pytest.raises(ConfigError, match="defaults") as exc_info:
        DropInSitesSource(tmp_path).load()
    assert str(path) in str(exc_info.value)


def test_drop_in_source_rejects_literal_api_token(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        '[[sites]]\nname = "acme"\nurl = "https://acme.atlassian.net"\napi_token = "leaked-token"\n'
    )
    with pytest.raises(ConfigError, match="api_token") as exc_info:
        DropInSitesSource(tmp_path).load()
    assert str(path) in str(exc_info.value)


def test_drop_in_source_rejects_literal_personal_token(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('[[sites]]\nname = "acme"\npersonal_token = "leaked-token"\n')
    with pytest.raises(ConfigError, match="personal_token"):
        DropInSitesSource(tmp_path).load()


def test_drop_in_source_accepts_the_env_form_of_token_fields(tmp_path: Path) -> None:
    path = tmp_path / "ok.toml"
    path.write_text(
        '[[sites]]\nname = "acme"\nurl = "https://acme.atlassian.net"\n'
        'key_prefixes = ["ACME"]\napi_token_env = "ACME_TOKEN"\n'
    )
    raw = DropInSitesSource(tmp_path).load()
    assert raw["sites"]["acme"]["api_token_env"] == "ACME_TOKEN"


def test_drop_in_source_rejects_duplicate_name_within_one_file(tmp_path: Path) -> None:
    path = tmp_path / "dupe.toml"
    path.write_text(
        '[[sites]]\nname = "acme"\nurl = "https://acme.atlassian.net"\n\n'
        '[[sites]]\nname = "acme"\nurl = "https://acme2.atlassian.net"\n'
    )
    with pytest.raises(ConfigError, match="duplicate"):
        DropInSitesSource(tmp_path).load()


def test_drop_in_source_rejects_sites_table_instead_of_array(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('[sites]\nacme = "oops"\n')
    with pytest.raises(ConfigError, match="array") as exc_info:
        DropInSitesSource(tmp_path).load()
    assert str(path) in str(exc_info.value)
