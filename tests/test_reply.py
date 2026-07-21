"""Unit tests for the reply-handling helpers extracted from Bridge.

These are the pure pieces of _format_reply_prefix / _strip_reply_prefix:
the relaymsg-suffixed target nick, the quote/ping prefix assembly (including
excerpt truncation), and the reply-fallback stripping.
"""

from j2i.bridge import (
    _REPLY_QUOTE_MAX,
    _build_reply_prefix,
    _relaymsg_nick,
    _strip_nick_prefix,
)


class TestRelaymsgNick:
    def test_appends_separator_and_suffix(self):
        assert _relaymsg_nick("alice", "/", "bridge") == "alice/bridge"

    def test_sanitizes_the_nick_first(self):
        assert _relaymsg_nick("a lice!", "/", "b") == "a-lice/b"


class TestBuildReplyPrefix:
    def test_ping_style_is_bare_nick(self):
        assert _build_reply_prefix("alice", "ping", "some body") == "alice: "

    def test_quote_without_cached_body_falls_back_to_bare(self):
        assert _build_reply_prefix("alice", "quote", None) == "alice: "

    def test_quote_with_body_includes_excerpt(self):
        assert (
            _build_reply_prefix("alice", "quote", "hi there")
            == '(re alice: "hi there") '
        )

    def test_newlines_flattened_to_spaces(self):
        assert (
            _build_reply_prefix("a", "quote", "line1\nline2")
            == '(re a: "line1 line2") '
        )

    def test_surrounding_whitespace_stripped(self):
        assert _build_reply_prefix("a", "quote", "  hi  ") == '(re a: "hi") '

    def test_excerpt_at_limit_is_not_truncated(self):
        body = "x" * _REPLY_QUOTE_MAX
        assert "…" not in _build_reply_prefix("a", "quote", body)
        assert _build_reply_prefix("a", "quote", body) == f'(re a: "{body}") '

    def test_excerpt_over_limit_truncated_with_ellipsis(self):
        body = "x" * (_REPLY_QUOTE_MAX + 10)
        expected_excerpt = "x" * (_REPLY_QUOTE_MAX - 1) + "…"
        assert (
            _build_reply_prefix("a", "quote", body)
            == f'(re a: "{expected_excerpt}") '
        )


class TestStripNickPrefix:
    def test_strips_matching_leading_prefix(self):
        assert _strip_nick_prefix("alice: hello", "alice") == "hello"

    def test_leaves_non_matching_nick(self):
        assert _strip_nick_prefix("bob: hello", "alice") == "bob: hello"

    def test_none_nick_is_noop(self):
        assert _strip_nick_prefix("alice: hello", None) == "alice: hello"

    def test_requires_colon_space_separator(self):
        # "alice hello" has no "alice: " prefix.
        assert _strip_nick_prefix("alice hello", "alice") == "alice hello"

    def test_only_strips_at_start(self):
        assert _strip_nick_prefix("x alice: y", "alice") == "x alice: y"
