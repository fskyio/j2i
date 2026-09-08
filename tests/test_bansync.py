"""Unit tests for XMPP→IRC puppet ban syncing."""

import asyncio
from xml.etree.ElementTree import Element, SubElement

from slixmpp import Presence

from j2i.bridge import Bridge, _KICK_REASON_MAX, _ban_kick_reason
from j2i.config import (
    BridgeMapping,
    Config,
    IRCConfig,
    Settings,
    XMPPConfig,
    load_config,
)
from j2i.xmpp.component import XMPPComponent, _NS_MUC_USER, _muc_user_info

_MUC = "general@conference.example.com"
_PJID = "alice.libera@irc.example.org"


def _muc_user_xml(
    codes: list[str],
    reason: str | None = None,
    actor: str | None = None,
    actor_jid: str | None = None,
) -> Element:
    root = Element("presence")
    x = SubElement(root, f"{{{_NS_MUC_USER}}}x")
    item = SubElement(x, f"{{{_NS_MUC_USER}}}item")
    if actor or actor_jid:
        actor_el = SubElement(item, f"{{{_NS_MUC_USER}}}actor")
        if actor:
            actor_el.set("nick", actor)
        if actor_jid:
            actor_el.set("jid", actor_jid)
    if reason is not None:
        reason_el = SubElement(item, f"{{{_NS_MUC_USER}}}reason")
        reason_el.text = reason
    for code in codes:
        status = SubElement(x, f"{{{_NS_MUC_USER}}}status")
        status.set("code", code)
    return root


def _presence(
    *,
    to: str,
    frm: str,
    codes: list[str],
    reason: str | None = None,
    actor: str | None = None,
) -> Presence:
    pres = Presence()
    pres["to"] = to
    pres["from"] = frm
    pres["type"] = "unavailable"
    x = SubElement(pres.xml, f"{{{_NS_MUC_USER}}}x")
    item = SubElement(x, f"{{{_NS_MUC_USER}}}item")
    if "301" in codes:
        item.set("affiliation", "outcast")
        item.set("role", "none")
    elif "307" in codes:
        item.set("role", "none")
    if actor:
        actor_el = SubElement(item, f"{{{_NS_MUC_USER}}}actor")
        actor_el.set("nick", actor)
    if reason is not None:
        reason_el = SubElement(item, f"{{{_NS_MUC_USER}}}reason")
        reason_el.text = reason
    for code in codes:
        status = SubElement(x, f"{{{_NS_MUC_USER}}}status")
        status.set("code", code)
    return pres


class TestMucUserInfo:
    def test_parses_ban_code_reason_and_actor(self):
        xml = _muc_user_xml(["301", "110"], reason="trolling", actor="mod")
        codes, reason, actor = _muc_user_info(xml)
        assert codes == {"301", "110"}
        assert reason == "trolling"
        assert actor == "mod"

    def test_actor_falls_back_to_jid(self):
        xml = _muc_user_xml(["301"], actor_jid="mod@example.com")
        _, _, actor = _muc_user_info(xml)
        assert actor == "mod@example.com"

    def test_missing_muc_user_is_empty(self):
        codes, reason, actor = _muc_user_info(Element("presence"))
        assert codes == set()
        assert reason is None
        assert actor is None

    def test_blank_reason_is_none(self):
        xml = _muc_user_xml(["301"], reason="   ")
        _, reason, _ = _muc_user_info(xml)
        assert reason is None


class TestBanKickReason:
    def test_default(self):
        assert _ban_kick_reason(None, None) == "Banned from XMPP MUC"

    def test_reason_only(self):
        assert _ban_kick_reason("trolling", None) == "Banned from XMPP: trolling"

    def test_actor_only(self):
        assert _ban_kick_reason(None, "mod") == "Banned from XMPP by mod"

    def test_actor_and_reason(self):
        assert (
            _ban_kick_reason("trolling", "mod")
            == "Banned from XMPP (mod): trolling"
        )

    def test_flattens_newlines(self):
        assert "\n" not in _ban_kick_reason("a\nb", None)

    def test_truncates_long_reason(self):
        text = _ban_kick_reason("x" * 200, "mod")
        assert len(text) == _KICK_REASON_MAX
        assert text.endswith("…")


class TestSyncBansConfig:
    def _bridge(
        self,
        *,
        global_val: bool = False,
        xmpp_val: bool | None = None,
        irc_val: bool | None = None,
        bridge_val: bool | None = None,
    ) -> tuple[Bridge, BridgeMapping]:
        mapping = BridgeMapping(
            xmpp="x",
            xmpp_muc=_MUC,
            irc="net",
            irc_channel="#c",
            sync_bans=bridge_val,
        )
        cfg = Config(
            xmpp=[
                XMPPConfig(
                    name="x",
                    password="p",
                    component=True,
                    component_domain="irc.example.org",
                    sync_bans=xmpp_val,
                )
            ],
            irc=[
                IRCConfig(
                    name="net", host="h", nick="bot", sync_bans=irc_val
                )
            ],
            bridges=[mapping],
            settings=Settings(sync_bans=global_val),
        )
        return Bridge(cfg), mapping

    def test_default_is_off(self):
        bridge, mapping = self._bridge()
        assert bridge._sync_bans(mapping) is False

    def test_global_on(self):
        bridge, mapping = self._bridge(global_val=True)
        assert bridge._sync_bans(mapping) is True

    def test_xmpp_overrides_global(self):
        bridge, mapping = self._bridge(global_val=True, xmpp_val=False)
        assert bridge._sync_bans(mapping) is False

    def test_irc_overrides_xmpp(self):
        bridge, mapping = self._bridge(xmpp_val=True, irc_val=False)
        assert bridge._sync_bans(mapping) is False

    def test_bridge_overrides_all(self):
        bridge, mapping = self._bridge(
            global_val=False, xmpp_val=False, irc_val=False, bridge_val=True
        )
        assert bridge._sync_bans(mapping) is True

    def test_load_config_parses_layers(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            """
[[xmpp]]
name = "x"
jid = "a@b.c"
password = "p"
sync_bans = true

[[irc]]
name = "i"
host = "h"
nick = "n"

[[bridge]]
xmpp = "x"
xmpp_muc = "m@r"
irc = "i"
irc_channel = "#c"
sync_bans = false

[settings]
sync_bans = true
""",
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.settings.sync_bans is True
        assert cfg.xmpp[0].sync_bans is True
        assert cfg.irc[0].sync_bans is None
        assert cfg.bridges[0].sync_bans is False


class TestPuppetUnavailable:
    def _component(self, muc_nick: str = "Alice") -> XMPPComponent:
        component = XMPPComponent("irc.example.org", "secret")
        component._track_puppet(_MUC, _PJID, irc_nick="Alice", muc_nick=muc_nick)
        return component

    def test_ban_untracks_and_keeps_original_irc_nick(self):
        got: list[tuple] = []

        async def capture(*args):
            got.append(args)

        async def run() -> XMPPComponent:
            component = self._component(muc_nick="Alice [deadbee]")
            component.on_puppet_banned = capture
            pres = _presence(
                to=_PJID,
                frm=f"{_MUC}/Alice [deadbee]",
                codes=["301", "110"],
                reason="trolling",
                actor="mod",
            )
            component._on_puppet_unavailable(pres)
            await asyncio.sleep(0)
            return component

        component = asyncio.run(run())
        assert got == [(_MUC, _PJID, "Alice", "trolling", "mod")]
        assert _PJID not in component._puppet_nicks.get(_MUC, {})
        assert _PJID not in component._puppet_irc_nicks.get(_MUC, {})

    def test_ban_drops_tracking(self):
        component = self._component()
        pres = _presence(to=_PJID, frm=f"{_MUC}/Alice", codes=["301"])
        component._on_puppet_unavailable(pres)
        assert _PJID not in component._puppet_nicks.get(_MUC, {})
        assert _PJID not in component._puppet_irc_nicks.get(_MUC, {})

    def test_kick_drops_tracking_without_callback(self):
        called = False

        async def should_not_run(*_args):
            nonlocal called
            called = True

        component = self._component()
        component.on_puppet_banned = should_not_run
        pres = _presence(to=_PJID, frm=f"{_MUC}/Alice", codes=["307"])
        component._on_puppet_unavailable(pres)
        assert _PJID not in component._puppet_nicks.get(_MUC, {})
        assert called is False

    def test_plain_leave_does_not_untrack(self):
        component = self._component()
        pres = _presence(to=_PJID, frm=f"{_MUC}/Alice", codes=["110"])
        component._on_puppet_unavailable(pres)
        assert _PJID in component._puppet_nicks[_MUC]

    def test_unknown_puppet_is_ignored(self):
        component = XMPPComponent("irc.example.org", "secret")
        pres = _presence(to=_PJID, frm=f"{_MUC}/Alice", codes=["301"])
        component._on_puppet_unavailable(pres)  # must not raise

    def test_filter_routes_puppet_ban_not_master(self):
        component = self._component()
        master_pres = _presence(
            to=component._master_jid,
            frm=f"{_MUC}/IRC Bridge",
            codes=["301"],
        )
        component._filter_presence_errors(master_pres)
        assert _PJID in component._puppet_nicks[_MUC]

        puppet_pres = _presence(to=_PJID, frm=f"{_MUC}/Alice", codes=["301"])
        component._filter_presence_errors(puppet_pres)
        assert _PJID not in component._puppet_nicks.get(_MUC, {})


class TestBridgeBanHandler:
    def _setup(
        self, *, sync_bans: bool
    ) -> tuple[Bridge, list[tuple[str, str, str]]]:
        mapping = BridgeMapping(
            xmpp="x",
            xmpp_muc=_MUC,
            irc="net",
            irc_channel="#general",
            sync_bans=sync_bans,
        )
        cfg = Config(
            xmpp=[
                XMPPConfig(
                    name="x",
                    password="p",
                    component=True,
                    component_domain="irc.example.org",
                )
            ],
            irc=[IRCConfig(name="net", host="h", nick="bot")],
            bridges=[mapping],
        )
        bridge = Bridge(cfg)
        sent: list[tuple[str, str, str]] = []

        class FakeIRC:
            async def ban_and_kick(self, channel, nick, reason):
                sent.append((channel, nick, reason))

        bridge.irc_clients["net"] = FakeIRC()  # type: ignore[assignment]
        bridge._xmpp_to_bridges[("x", _MUC)] = [mapping]
        return bridge, sent

    def test_syncs_when_enabled(self):
        bridge, sent = self._setup(sync_bans=True)
        handler = bridge._make_puppet_banned_handler("x")
        asyncio.run(
            handler(_MUC.upper(), _PJID, "Alice", "trolling", "mod")
        )
        assert sent == [
            ("#general", "Alice", "Banned from XMPP (mod): trolling")
        ]

    def test_skips_when_disabled(self):
        bridge, sent = self._setup(sync_bans=False)
        handler = bridge._make_puppet_banned_handler("x")
        asyncio.run(handler(_MUC, _PJID, "Alice", None, None))
        assert sent == []

    def test_missing_irc_client_does_not_raise(self):
        bridge, sent = self._setup(sync_bans=True)
        bridge.irc_clients.clear()
        handler = bridge._make_puppet_banned_handler("x")
        asyncio.run(handler(_MUC, _PJID, "Alice", None, None))
        assert sent == []

    def test_ban_and_kick_failure_is_swallowed(self):
        bridge, _sent = self._setup(sync_bans=True)

        class Boom:
            async def ban_and_kick(self, channel, nick, reason):
                raise RuntimeError("nope")

        bridge.irc_clients["net"] = Boom()  # type: ignore[assignment]
        handler = bridge._make_puppet_banned_handler("x")
        asyncio.run(handler(_MUC, _PJID, "Alice", None, None))
