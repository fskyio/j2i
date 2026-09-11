"""Config loading and validation for IRC nick-puppet settings."""

from pathlib import Path

import pytest

from j2i.config import (
    IRCConfig,
    Settings,
    load_config,
    resolve_max_puppets,
    resolve_puppet_mode,
    resolve_puppet_suffix,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


_BASE = """
[[xmpp]]
name = "x"
jid = "bot@example.com"
password = "secret"

[[irc]]
name = "net"
host = "irc.example.org"
nick = "bridge"

[[bridge]]
xmpp = "x"
xmpp_muc = "room@conference.example.com"
irc = "net"
irc_channel = "#chan"
"""


class TestDefaultsPreserveRelaymsg:
    def test_omitted_keys_keep_relaymsg_mode(self, tmp_path: Path):
        cfg = load_config(_write(tmp_path, _BASE))
        assert cfg.settings.irc_puppet_mode == "relaymsg"
        assert cfg.settings.irc_max_puppets == 16
        assert cfg.settings.irc_puppet_idle_seconds == 600
        assert cfg.settings.irc_puppet_suffix is None
        assert cfg.settings.irc_puppet_separator == "|"
        irc = cfg.irc_by_name("net")
        assert irc.puppet_mode is None
        assert resolve_puppet_mode(irc, cfg.settings) == "relaymsg"

    def test_suffix_inherits_relaymsg_suffix(self, tmp_path: Path):
        cfg = load_config(_write(tmp_path, _BASE + "\n[settings]\nrelaymsg_suffix = \"bridge\"\n"))
        irc = cfg.irc_by_name("net")
        assert resolve_puppet_suffix(irc, cfg.settings) == "bridge"


class TestPerNetworkOverride:
    def test_nicks_mode_and_unlimited_pool(self, tmp_path: Path):
        body = _BASE.replace(
            'nick = "bridge"',
            'nick = "bridge"\npuppet_mode = "nicks"\nmax_puppets = 0\n'
            'puppet_idle_seconds = 0\npuppet_suffix = ""\n',
        )
        cfg = load_config(_write(tmp_path, body))
        irc = cfg.irc_by_name("net")
        assert resolve_puppet_mode(irc, cfg.settings) == "nicks"
        assert resolve_max_puppets(irc, cfg.settings) == 0
        assert resolve_puppet_suffix(irc, cfg.settings) == ""


class TestValidation:
    def test_bad_mode_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="puppet_mode"):
            load_config(
                _write(tmp_path, _BASE + "\n[settings]\nirc_puppet_mode = \"plumbing\"\n")
            )

    def test_slash_separator_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="separator"):
            load_config(
                _write(tmp_path, _BASE + "\n[settings]\nirc_puppet_separator = \"/\"\n")
            )

    def test_negative_max_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="max_puppets"):
            load_config(
                _write(tmp_path, _BASE + "\n[settings]\nirc_max_puppets = -1\n")
            )
