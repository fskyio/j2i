"""Unit tests for MUC occupant presence classification."""

from j2i.xmpp.presence import parse_occupant_event


class TestParseOccupantEvent:
    def test_join_available(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="available",
            self_nick="bot",
            codes=set(),
        )
        assert ev is not None
        assert ev.kind == "join"
        assert ev.nick == "alice"
        assert ev.is_self is False
        assert ev.away_reason is None

    def test_join_empty_type(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="",
            self_nick="bot",
            codes=set(),
        )
        assert ev is not None
        assert ev.kind == "join"

    def test_away_show(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="available",
            self_nick="bot",
            codes=set(),
            show="xa",
            status="gone",
        )
        assert ev is not None
        assert ev.kind == "join"
        assert ev.away_reason == "gone"

    def test_dnd_is_away(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="available",
            self_nick="bot",
            codes=set(),
            show="dnd",
        )
        assert ev is not None
        assert ev.away_reason == "dnd"

    def test_leave(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="unavailable",
            self_nick="bot",
            codes=set(),
        )
        assert ev is not None
        assert ev.kind == "leave"
        assert ev.is_self is False

    def test_kick_307_keeps_reason(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="unavailable",
            self_nick="bot",
            codes={"307"},
            reason="trolling",
            actor="mod",
        )
        assert ev is not None
        assert ev.kind == "leave"
        assert "307" in ev.status_codes
        assert ev.reason == "trolling"
        assert ev.actor == "mod"

    def test_nick_change_303(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="unavailable",
            self_nick="bot",
            codes={"303"},
            item_nick="alison",
        )
        assert ev is not None
        assert ev.kind == "nick"
        assert ev.nick == "alice"
        assert ev.new_nick == "alison"

    def test_303_without_item_nick_is_leave(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="unavailable",
            self_nick="bot",
            codes={"303"},
        )
        assert ev is not None
        assert ev.kind == "leave"

    def test_self_by_code_110(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="bot",
            ptype="unavailable",
            self_nick="other",
            codes={"110", "307"},
        )
        assert ev is not None
        assert ev.is_self is True
        assert ev.kind == "leave"
        assert "307" in ev.status_codes

    def test_self_by_nick(self):
        ev = parse_occupant_event(
            muc_jid="room@c",
            nick="bot",
            ptype="available",
            self_nick="bot",
            codes=set(),
        )
        assert ev is not None
        assert ev.is_self is True
        assert ev.kind == "join"

    def test_empty_nick_ignored(self):
        assert parse_occupant_event(
            muc_jid="room@c",
            nick="",
            ptype="available",
            self_nick="bot",
            codes=set(),
        ) is None

    def test_error_ignored(self):
        assert parse_occupant_event(
            muc_jid="room@c",
            nick="alice",
            ptype="error",
            self_nick="bot",
            codes=set(),
        ) is None
