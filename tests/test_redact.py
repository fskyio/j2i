"""Unit tests for _redact_for_log: masking credentials in outbound IRC
lines before they reach the debug log, while keeping the command visible.
"""

from j2i.irc.client import _redact_for_log


class TestSasl:
    def test_masks_authenticate_payload(self):
        assert (
            _redact_for_log("AUTHENTICATE aGVsbG8AaGVsbG8AaHVudGVyMg==")
            == "AUTHENTICATE <redacted>"
        )

    def test_keeps_mechanism_line(self):
        assert _redact_for_log("AUTHENTICATE PLAIN") == "AUTHENTICATE PLAIN"

    def test_keeps_continuation_tokens(self):
        assert _redact_for_log("AUTHENTICATE +") == "AUTHENTICATE +"
        assert _redact_for_log("AUTHENTICATE *") == "AUTHENTICATE *"


class TestNickServ:
    def test_masks_identify_password(self):
        assert (
            _redact_for_log("PRIVMSG NickServ :IDENTIFY hunter2")
            == "PRIVMSG NickServ :IDENTIFY <redacted>"
        )

    def test_masks_ns_shorthand(self):
        assert _redact_for_log("NS IDENTIFY hunter2") == "NS IDENTIFY <redacted>"


class TestPass:
    def test_masks_server_password(self):
        assert _redact_for_log("PASS s3cret") == "PASS <redacted>"


class TestPassthrough:
    def test_leaves_ordinary_lines_untouched(self):
        line = "PRIVMSG #chan :hello there"
        assert _redact_for_log(line) == line

    def test_does_not_mask_the_word_identify_alone(self):
        # No NickServ/NS context — not a credential line.
        line = "PRIVMSG #chan :please identify yourself"
        assert _redact_for_log(line) == line
