"""Unit tests for IRC→XMPP mention rewriting and XEP-0513 XML."""

from xml.etree.ElementTree import Element

from j2i.irc.nicks import casemap_nick
from j2i.xmpp.mentions import (
    Mention,
    MentionTarget,
    attach_mentions,
    rewrite_irc_mentions,
    shift_mentions,
)


class TestRewriteIrcMentions:
    def test_puppet_nick_rewritten_to_xmpp_nick(self):
        targets = {
            "alice|xmpp": MentionTarget("alice", "room@c/alice", "oid-a"),
        }
        text, mentions = rewrite_irc_mentions(
            "hello alice|xmpp how are you", targets
        )
        assert text == "hello alice how are you"
        assert mentions == [
            Mention(6, 11, "room@c/alice", "oid-a"),
        ]

    def test_colon_ping(self):
        targets = {
            "alice|xmpp": MentionTarget("alice", "room@c/alice", None),
        }
        text, mentions = rewrite_irc_mentions("alice|xmpp: hi", targets)
        assert text == "alice: hi"
        assert mentions[0].begin == 0
        assert mentions[0].end == 5

    def test_irc_user_keeps_body_and_mentions_puppet(self):
        targets = {"bob": MentionTarget("bob", "room@c/bob", None)}
        text, mentions = rewrite_irc_mentions("hey bob", targets)
        assert text == "hey bob"
        assert mentions == [Mention(4, 7, "room@c/bob", None)]

    def test_collision_muc_nick_as_fallback(self):
        targets = {"bob": MentionTarget("bob [abc1234]", "room@c/bob [abc1234]", "id")}
        text, mentions = rewrite_irc_mentions("hey bob", targets)
        assert text == "hey bob [abc1234]"
        assert mentions[0].begin == 4
        assert mentions[0].end == len("hey bob [abc1234]")

    def test_does_not_match_substring(self):
        targets = {"alice": MentionTarget("alice", "room@c/alice", None)}
        text, mentions = rewrite_irc_mentions("malice", targets)
        assert text == "malice"
        assert mentions == []

    def test_full_puppet_token_not_bare_nick(self):
        targets = {
            "alice": MentionTarget("wrong", "room@c/wrong", "no"),
            "alice|xmpp": MentionTarget("alice", "room@c/alice", "yes"),
        }
        text, mentions = rewrite_irc_mentions("hi alice|xmpp", targets)
        assert text == "hi alice"
        assert mentions[0].occupant_id == "yes"

    def test_skips_urls(self):
        targets = {
            "alice|xmpp": MentionTarget("alice", "room@c/alice", None),
        }
        original = "see https://example.com/alice|xmpp please"
        text, mentions = rewrite_irc_mentions(original, targets)
        assert text == original
        assert mentions == []

    def test_relaymsg_slash_nick(self):
        targets = {
            "alice/xmpp": MentionTarget("alice", "room@c/alice", None),
        }
        text, mentions = rewrite_irc_mentions("hi alice/xmpp", targets)
        assert text == "hi alice"
        assert mentions == [Mention(3, 8, "room@c/alice", None)]

    def test_rfc1459_pipe_casemap(self):
        key = casemap_nick("alice|xmpp", "rfc1459")
        targets = {key: MentionTarget("alice", "room@c/alice", None)}
        text, mentions = rewrite_irc_mentions(
            "hi alice|xmpp", targets, mapping="rfc1459"
        )
        assert text == "hi alice"
        assert mentions

    def test_multiple_mentions_shift_after_rewrite(self):
        targets = {
            "alice|xmpp": MentionTarget("alice", "r/alice", "a"),
            "bob": MentionTarget("bob", "r/bob", "b"),
        }
        text, mentions = rewrite_irc_mentions("alice|xmpp and bob", targets)
        assert text == "alice and bob"
        assert mentions == [
            Mention(0, 5, "r/alice", "a"),
            Mention(10, 13, "r/bob", "b"),
        ]

    def test_empty_targets_unchanged(self):
        assert rewrite_irc_mentions("hello alice|xmpp", {}) == (
            "hello alice|xmpp", []
        )


class TestShiftMentions:
    def test_action_prefix(self):
        shifted = shift_mentions(
            [Mention(6, 11, "r/a", "id")], 4
        )
        assert shifted == [Mention(10, 15, "r/a", "id")]

    def test_zero_delta_returns_same(self):
        original = [Mention(1, 2, "r/a", None)]
        assert shift_mentions(original, 0) is original


class TestAttachMentions:
    def test_occupantid_when_known(self):
        msg = type("M", (), {"xml": Element("message")})()
        attach_mentions(msg, [Mention(6, 11, "room@c/alice", "oid-a")])
        el = msg.xml.find("{urn:xmpp:mentions:0}mention")
        assert el is not None
        assert el.get("begin") == "6"
        assert el.get("end") == "11"
        assert el.get("occupantid") == "oid-a"
        assert el.get("jid") is None

    def test_jid_fallback(self):
        msg = type("M", (), {"xml": Element("message")})()
        attach_mentions(msg, [Mention(4, 7, "room@c/bob", None)])
        el = msg.xml.find("{urn:xmpp:mentions:0}mention")
        assert el is not None
        assert el.get("jid") == "room@c/bob"
        assert el.get("occupantid") is None
