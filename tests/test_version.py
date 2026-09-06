"""Unit tests for software identity helpers (no XMPP I/O)."""

from j2i.version import (
    CAPS_NODE,
    SOFTWARE_NAME,
    software_label,
    software_os,
    software_version,
    xep_0092_config,
)
from j2i.xmpp.client import XMPPClient
from j2i.xmpp.component import XMPPComponent


class TestSoftwareLabel:
    def test_includes_package_version(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        assert software_label() == "j2i 1.2.0"

    def test_hide_version_is_name_only(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        assert software_label(hide_version=True) == SOFTWARE_NAME

    def test_missing_package_version_is_name_only(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "")
        assert software_label() == SOFTWARE_NAME


class TestSoftwareVersion:
    def test_returns_package_version(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        assert software_version() == "1.2.0"

    def test_hide_version_is_empty(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        assert software_version(hide_version=True) == ""


class TestSoftwareOs:
    def test_uses_platform_system(self, monkeypatch):
        monkeypatch.setattr("j2i.version.platform.system", lambda: "Linux")
        assert software_os() == "Linux"

    def test_hide_os_is_empty(self, monkeypatch):
        monkeypatch.setattr("j2i.version.platform.system", lambda: "Linux")
        assert software_os(hide_os=True) == ""


class TestXep0092Config:
    def test_full_announcement(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        monkeypatch.setattr("j2i.version.platform.system", lambda: "Linux")
        assert xep_0092_config() == {
            "software_name": "j2i",
            "version": "1.2.0",
            "os": "Linux",
        }

    def test_hidden_fields_are_empty(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        monkeypatch.setattr("j2i.version.platform.system", lambda: "Linux")
        assert xep_0092_config(hide_version=True, hide_os=True) == {
            "software_name": "j2i",
            "version": "",
            "os": "",
        }


class TestXmppSoftwareIdentity:
    def test_client_registers_0092_and_resource(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        monkeypatch.setattr("j2i.version.platform.system", lambda: "Linux")
        client = XMPPClient("bot@example.com", "pw")
        plugin = client._xmpp["xep_0092"]
        assert plugin.software_name == "j2i"
        assert plugin.version == "1.2.0"
        assert plugin.os == "Linux"
        assert client._xmpp.requested_jid.resource == "j2i 1.2.0"
        assert client._xmpp["xep_0115"].caps_node == CAPS_NODE

    def test_hide_flags_strip_version_and_os(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        monkeypatch.setattr("j2i.version.platform.system", lambda: "Linux")
        client = XMPPClient(
            "bot@example.com", "pw", hide_version=True, hide_os=True
        )
        plugin = client._xmpp["xep_0092"]
        assert plugin.software_name == "j2i"
        assert plugin.version == ""
        assert plugin.os == ""
        assert client._xmpp.requested_jid.resource == "j2i"

    def test_component_registers_0092(self, monkeypatch):
        monkeypatch.setattr("j2i.version.package_version", lambda: "1.2.0")
        monkeypatch.setattr("j2i.version.platform.system", lambda: "FreeBSD")
        component = XMPPComponent("irc.example.org", "secret")
        plugin = component._xmpp["xep_0092"]
        assert plugin.software_name == "j2i"
        assert plugin.version == "1.2.0"
        assert plugin.os == "FreeBSD"
        assert component._xmpp["xep_0115"].caps_node == CAPS_NODE
