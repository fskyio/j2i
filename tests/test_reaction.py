"""Unit tests for the reaction text builder and shared excerpt primitive."""

from j2i.bridge import (
    _REPLY_QUOTE_MAX,
    _build_reaction_text,
    _excerpt,
)


class TestExcerpt:
    def test_flattens_newlines(self):
        assert _excerpt("line1\nline2") == "line1 line2"

    def test_strips_surrounding_whitespace(self):
        assert _excerpt("  hi  ") == "hi"

    def test_empty_stays_empty(self):
        assert _excerpt("") == ""

    def test_at_limit_not_truncated(self):
        body = "x" * _REPLY_QUOTE_MAX
        assert _excerpt(body) == body

    def test_over_limit_truncated_with_ellipsis(self):
        body = "x" * (_REPLY_QUOTE_MAX + 5)
        assert _excerpt(body) == "x" * (_REPLY_QUOTE_MAX - 1) + "…"


class TestBuildReactionText:
    def test_ping_style_is_bare_reacted(self):
        assert _build_reaction_text("👍", "ping", "some body") == "reacted 👍"

    def test_quote_without_cached_body_falls_back(self):
        assert _build_reaction_text("👍", "quote", None) == "reacted 👍"

    def test_quote_with_body_includes_excerpt(self):
        assert (
            _build_reaction_text("👍", "quote", "hello there")
            == '(reacted to "hello there") 👍'
        )

    def test_newlines_flattened(self):
        assert (
            _build_reaction_text("🎉", "quote", "line1\nline2")
            == '(reacted to "line1 line2") 🎉'
        )

    def test_long_body_truncated(self):
        body = "x" * (_REPLY_QUOTE_MAX + 10)
        expected = "x" * (_REPLY_QUOTE_MAX - 1) + "…"
        assert (
            _build_reaction_text("👍", "quote", body)
            == f'(reacted to "{expected}") 👍'
        )
