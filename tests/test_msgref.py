"""Scoped message-reference keys and cache isolation across rooms/networks."""

import collections
from xml.etree.ElementTree import Element

from j2i.bridge import (
    Bridge,
    MsgRef,
    irc_ref,
    xmpp_ref,
)
from j2i.config import BridgeMapping, Config, IRCConfig, Settings
from j2i.irc.client import IRCClient
from j2i.xmpp.client import XMPPMessage, extract_stanza_id


class TestMsgRef:
    def test_room_is_lowercased(self):
        ref = MsgRef(conn="x", room="Room@Conf.Example", id="sid")
        assert ref.room == "room@conf.example"

    def test_equality_ignores_by(self):
        a = MsgRef(conn="x", room="room@c", id="sid", by="room@c")
        b = MsgRef(conn="x", room="ROOM@c", id="sid", by=None)
        assert a == b
        assert hash(a) == hash(b)

    def test_different_rooms_are_distinct(self):
        a = xmpp_ref("x", "room-a@c", "sid")
        b = xmpp_ref("x", "room-b@c", "sid")
        assert a != b
        assert hash(a) != hash(b)

    def test_different_connections_are_distinct(self):
        a = irc_ref("libera", "#chan", "msgid-1")
        b = irc_ref("oftc", "#chan", "msgid-1")
        assert a != b

    def test_usable_as_dict_key_without_by(self):
        stored = xmpp_ref("x", "room@c", "sid", by="room@c")
        lookup = xmpp_ref("x", "room@c", "sid")
        cache = {stored: "body"}
        assert cache[lookup] == "body"


class TestExtractStanzaId:
    def test_returns_id_and_by(self):
        root = Element("message")
        el = Element("{urn:xmpp:sid:0}stanza-id")
        el.set("id", "abc")
        el.set("by", "room@c")
        root.append(el)

        class _Msg:
            xml = root

        assert extract_stanza_id(_Msg()) == ("abc", "room@c")

    def test_missing_element_returns_nones(self):
        class _Msg:
            xml = Element("message")

        assert extract_stanza_id(_Msg()) == (None, None)


class TestCacheIsolation:
    def test_body_cache_does_not_cross_mucs(self):
        bridge = Bridge(Config())
        same_id = "sid-1"
        bridge._cache_body(xmpp_ref("x", "room-a@c", same_id, by="room-a@c"), "body A")
        bridge._cache_body(xmpp_ref("x", "room-b@c", same_id, by="room-b@c"), "body B")
        assert bridge._body_cache[xmpp_ref("x", "room-a@c", same_id)] == "body A"
        assert bridge._body_cache[xmpp_ref("x", "room-b@c", same_id)] == "body B"

    def test_irc_msgid_map_does_not_cross_networks(self):
        bridge = Bridge(Config())
        sid_a = xmpp_ref("x", "a@c", "sid-a")
        sid_b = xmpp_ref("x", "b@c", "sid-b")
        bridge._irc_msgid_to_xmpp_sid[irc_ref("libera", "#foo", "msgid-1")] = sid_a
        bridge._irc_msgid_to_xmpp_sid[irc_ref("oftc", "#foo", "msgid-1")] = sid_b
        assert (
            bridge._irc_msgid_to_xmpp_sid[irc_ref("libera", "#foo", "msgid-1")].id
            == "sid-a"
        )
        assert (
            bridge._irc_msgid_to_xmpp_sid[irc_ref("oftc", "#foo", "msgid-1")].id
            == "sid-b"
        )

    async def test_self_message_rekeys_only_matching_room(self):
        bridge = Bridge(Config())
        cid_a = xmpp_ref("x1", "a@c", "same-cid")
        cid_b = xmpp_ref("x1", "b@c", "same-cid")
        bridge._record_msg_id(cid_a, "alice")
        bridge._record_msg_id(cid_b, "bob")
        bridge._pending_body[cid_a] = "hi from a"
        bridge._pending_body[cid_b] = "hi from b"

        handler = bridge._make_xmpp_self_msg_handler("x1")
        await handler("a@c", "same-cid", "sid-a", "a@c")

        sid_a = xmpp_ref("x1", "a@c", "sid-a")
        assert bridge._msg_id_to_irc_nick[sid_a] == "alice"
        assert cid_a not in bridge._msg_id_to_irc_nick
        assert bridge._msg_id_to_irc_nick[cid_b] == "bob"
        assert bridge._body_cache[sid_a] == "hi from a"
        assert bridge._pending_body[cid_b] == "hi from b"

    async def test_irc_echo_round_trip_is_room_scoped(self):
        bridge = Bridge(Config())
        sid = xmpp_ref("x1", "room@c", "sid-1", by="room@c")
        echo_key = ("libera", "#chan")
        bridge._pending_echo_sids[echo_key] = collections.deque([sid])

        handler = bridge._make_irc_self_msg_handler("libera")
        await handler("#chan", "irc-msgid-1")

        irc = irc_ref("libera", "#chan", "irc-msgid-1")
        assert bridge._irc_msgid_to_xmpp_sid[irc] == sid
        assert bridge._xmpp_sid_to_irc_msgid[sid].id == "irc-msgid-1"
        # Same msgid on another network stays unmapped.
        assert irc_ref("oftc", "#chan", "irc-msgid-1") not in bridge._irc_msgid_to_xmpp_sid

    def test_reply_prefix_uses_originating_room_body_and_nick(self):
        cfg = Config(settings=Settings(reply_style="quote", anti_ping=False))
        bridge = Bridge(cfg)
        same_id = "sid-1"
        bridge._cache_body(xmpp_ref("x", "room-a@c", same_id, by="room-a@c"), "body A")
        bridge._cache_body(xmpp_ref("x", "room-b@c", same_id, by="room-b@c"), "body B")
        bridge._msg_id_to_irc_nick[xmpp_ref("x", "room-a@c", same_id)] = "alice"
        bridge._msg_id_to_irc_nick[xmpp_ref("x", "room-b@c", same_id)] = "bob"

        client = IRCClient(host="h", port=6697, nick="bot")
        irc_cfg = IRCConfig(name="net", host="h", nick="bot", relaymsg=False)
        mapping = BridgeMapping(
            xmpp="x", xmpp_muc="room-a@c", irc="net", irc_channel="#c",
            anti_ping=False,
        )
        msg = XMPPMessage(
            muc_jid="room-a@c",
            nick="carol",
            body="reply",
            reply_to_nick="whoever",
            reply_to_id=same_id,
        )
        prefix = bridge._format_reply_prefix(msg, client, irc_cfg, mapping, xmpp_name="x")
        assert "alice" in prefix
        assert "body A" in prefix
        assert "bob" not in prefix
        assert "body B" not in prefix

    def test_reaction_text_uses_originating_room_body(self):
        cfg = Config(settings=Settings(reply_style="quote"))
        bridge = Bridge(cfg)
        same_id = "sid-1"
        bridge._cache_body(xmpp_ref("x", "room-a@c", same_id), "body A")
        bridge._cache_body(xmpp_ref("x", "room-b@c", same_id), "body B")
        mapping = BridgeMapping(
            xmpp="x", xmpp_muc="room-a@c", irc="net", irc_channel="#c",
        )
        text = bridge._format_reaction_text(
            mapping, xmpp_ref("x", "room-a@c", same_id), "👍"
        )
        assert "body A" in text
        assert "body B" not in text
