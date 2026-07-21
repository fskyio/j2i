"""Unit tests for the pure translation helpers in j2i.bridge.

These cover the nick-mangling and IRC->XMPP formatting logic that has the
highest bug surface and no external dependencies.
"""

import pytest

from j2i.bridge import (
    ANTI_PING_CHAR,
    anti_ping,
    format_irc_to_xmpp,
    sanitize_irc_nick,
    _puppet_jid,
)


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
