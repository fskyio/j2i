"""Unit tests for the pure translation helpers in j2i.bridge.

These cover the nick-mangling and IRC->XMPP formatting logic that has the
highest bug surface and no external dependencies.
"""

import pytest

from j2i.bridge import (
    ANTI_PING_CHAR,
    Bridge,
    anti_ping,
    format_irc_to_xmpp,
    sanitize_irc_nick,
    _puppet_jid,
    _split_to_byte_limit,
)
from j2i.config import BridgeMapping, Config, IRCConfig, Settings
from j2i.irc.client import IRCClient
from j2i.xmpp.client import XMPPMessage


class TestAntiPing:
    def test_inserts_zero_width_space_in_middle(self):
        assert anti_ping("abcd") == "ab" + ANTI_PING_CHAR + "cd"

    def test_odd_length_splits_before_middle_char(self):
        assert anti_ping("abc") == "a" + ANTI_PING_CHAR + "bc"

    def test_single_char_is_unchanged(self):
        # Too short to hide a highlight in; left alone.
        assert anti_ping("a") == "a"

    def test_empty_is_unchanged(self):
        assert anti_ping("") == ""

    def test_result_strips_back_to_original(self):
        nick = "SomeNick"
        assert anti_ping(nick).replace(ANTI_PING_CHAR, "") == nick


class TestSanitizeIrcNick:
    def test_plain_nick_untouched(self):
        assert sanitize_irc_nick("alice") == "alice"

    def test_irc_legal_punctuation_preserved(self):
        # Brackets/braces/backtick etc. are legal in IRC nicks.
        assert sanitize_irc_nick("[alice]`{}") == "[alice]`{}"

    def test_illegal_chars_become_dash(self):
        assert sanitize_irc_nick("a.b") == "a-b"

    def test_consecutive_illegal_chars_collapse_to_single_dash(self):
        assert sanitize_irc_nick("a!!!b") == "a-b"

    def test_leading_and_trailing_dashes_stripped(self):
        assert sanitize_irc_nick("-alice-") == "alice"

    def test_spaces_replaced(self):
        assert sanitize_irc_nick("hi there") == "hi-there"

    def test_all_illegal_falls_back_to_unknown(self):
        assert sanitize_irc_nick("!!!") == "unknown"

    def test_empty_falls_back_to_unknown(self):
        assert sanitize_irc_nick("") == "unknown"


class TestPuppetJid:
    def test_builds_lowercased_localpart_with_network(self):
        assert (
            _puppet_jid("Alice", "libera", "irc.example.org")
            == "alice.libera@irc.example.org"
        )

    def test_sanitizes_nick_before_building(self):
        assert (
            _puppet_jid("A.Lice!", "oftc", "bridge.test")
            == "a-lice.oftc@bridge.test"
        )


class TestFormatIrcToXmpp:
    def test_plain_text_passthrough(self):
        assert format_irc_to_xmpp("hello world") == "hello world"

    def test_bold_pair_becomes_asterisks(self):
        assert format_irc_to_xmpp("\x02bold\x02") == "*bold*"

    def test_unclosed_bold_is_auto_closed(self):
        # A dangling format toggle is closed at end of string.
        assert format_irc_to_xmpp("\x02bold") == "*bold*"

    def test_italic_becomes_underscores(self):
        assert format_irc_to_xmpp("\x1ditalic\x1d") == "_italic_"

    def test_strikethrough_becomes_tildes(self):
        assert format_irc_to_xmpp("\x1estrike\x1e") == "~strike~"

    def test_monospace_becomes_backticks(self):
        assert format_irc_to_xmpp("\x11code\x11") == "`code`"

    def test_reset_closes_open_formatting(self):
        assert format_irc_to_xmpp("\x02bold\x0fplain") == "*bold*plain"

    def test_nested_formatting_closed_in_order(self):
        # \x0f closes mono, strike, italic, bold in that order.
        assert format_irc_to_xmpp("\x02\x1dboth\x0f") == "*_both_*"

    def test_color_codes_are_stripped(self):
        assert format_irc_to_xmpp("\x0304,01red\x03") == "red"

    def test_color_code_without_digits_stripped(self):
        assert format_irc_to_xmpp("\x03plain") == "plain"

    def test_reverse_and_underline_controls_removed(self):
        assert format_irc_to_xmpp("\x16a\x1fb") == "ab"

    def test_stray_control_chars_stripped(self):
        assert format_irc_to_xmpp("a\x00\x07b") == "ab"

    @pytest.mark.parametrize("ws", ["\t", "\n", "\r"])
    def test_common_whitespace_preserved(self, ws):
        assert format_irc_to_xmpp(f"a{ws}b") == f"a{ws}b"


class TestSplitToByteLimit:
    def test_short_line_unchanged(self):
        assert _split_to_byte_limit("hello world", 100) == ["hello world"]

    def test_line_exactly_at_limit_unchanged(self):
        text = "a" * 10
        assert _split_to_byte_limit(text, 10) == [text]

    def test_zero_or_negative_limit_is_noop(self):
        assert _split_to_byte_limit("anything", 0) == ["anything"]
        assert _split_to_byte_limit("anything", -5) == ["anything"]

    def test_breaks_on_spaces(self):
        # "aaaa bbbb cccc" at limit 9 -> "aaaa bbbb" won't fit (9 ok, but
        # next word overflows), so it breaks at the last space that fits.
        chunks = _split_to_byte_limit("aaaa bbbb cccc", 9)
        assert chunks == ["aaaa bbbb", "cccc"]
        assert all(len(c.encode("utf-8")) <= 9 for c in chunks)

    def test_every_chunk_within_limit(self):
        text = " ".join(["word"] * 50)
        chunks = _split_to_byte_limit(text, 20)
        assert all(len(c.encode("utf-8")) <= 20 for c in chunks)
        assert " ".join(chunks) == text  # lossless up to the split spaces

    def test_hard_break_for_word_longer_than_limit(self):
        chunks = _split_to_byte_limit("x" * 25, 10)
        assert chunks == ["x" * 10, "x" * 10, "x" * 5]

    def test_never_splits_multibyte_character(self):
        # Each emoji is 4 UTF-8 bytes; a limit of 6 must not cut one in half.
        text = "😀😀😀"
        chunks = _split_to_byte_limit(text, 6)
        for c in chunks:
            assert len(c.encode("utf-8")) <= 6
            c.encode("utf-8").decode("utf-8")  # round-trips = no broken char
        assert "".join(chunks) == text

    def test_multibyte_word_break_reserves_boundary(self):
        # 3-byte chars, limit 7 -> at most 2 chars (6 bytes) per chunk.
        text = "€€€€€"
        chunks = _split_to_byte_limit(text, 7)
        assert chunks == ["€€", "€€", "€"]


class TestIrcBodyBudget:
    """Precedence of the max_line_bytes ceiling: bridge > network > global > auto."""

    def _budget(
        self,
        *,
        line_len: int = 512,
        global_mlb: int = 0,
        network_mlb: int | None = None,
        bridge_mlb: int | None = None,
    ) -> int:
        cfg = Config(settings=Settings(max_line_bytes=global_mlb))
        bridge = Bridge(cfg)
        client = IRCClient(host="h", port=6697, nick="bot")
        client.line_len = line_len
        irc_cfg = IRCConfig(
            name="net", host="h", nick="bot", max_line_bytes=network_mlb
        )
        b = BridgeMapping(
            xmpp="x", xmpp_muc="m", irc="net", irc_channel="#c",
            anti_ping=False, max_line_bytes=bridge_mlb,
        )
        msg = XMPPMessage(muc_jid="m", nick="alice", body="hi")
        # Fixed channel/nick/reply_prefix => constant overhead, so the budget
        # tracks the resolved ceiling one-for-one.
        return bridge._irc_body_budget(client, irc_cfg, "#c", msg, b, "")

    def test_unset_uses_auto_detected_line_len(self):
        low = self._budget(line_len=512)
        high = self._budget(line_len=1012)
        assert high - low == 500  # budget follows the auto-detected ceiling

    def test_global_overrides_auto(self):
        # Global ceiling below the auto-detected 512 tightens the budget.
        assert self._budget(line_len=900, global_mlb=512) == self._budget(line_len=512)

    def test_network_overrides_global(self):
        assert self._budget(global_mlb=512, network_mlb=712) == self._budget(
            line_len=712
        )

    def test_bridge_overrides_network_and_global(self):
        assert self._budget(
            global_mlb=512, network_mlb=712, bridge_mlb=912
        ) == self._budget(line_len=912)

    def test_zero_and_none_are_treated_as_unset(self):
        # Zero global + None network/bridge => falls through to auto-detect.
        assert self._budget(line_len=800, global_mlb=0) == self._budget(line_len=800)
