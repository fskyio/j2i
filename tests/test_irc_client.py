"""Unit tests for IRCClient ISUPPORT parsing relevant to line-length handling."""

import asyncio

from j2i.irc.client import IRCClient


def _client() -> IRCClient:
    return IRCClient(host="irc.example.org", port=6697, nick="bridge")


def _isupport(client: IRCClient, *tokens: str) -> None:
    # params mirror the wire form: our nick, tokens..., trailing description.
    params = ["bridge", *tokens, "are supported by this server"]
    asyncio.run(client._handle_isupport(params))


class TestLineLen:
    def test_defaults_to_512(self):
        assert _client().line_len == 512

    def test_linelen_token_overrides_default(self):
        client = _client()
        _isupport(client, "LINELEN=1024")
        assert client.line_len == 1024

    def test_non_numeric_linelen_ignored(self):
        client = _client()
        _isupport(client, "LINELEN=lots")
        assert client.line_len == 512

    def test_zero_linelen_ignored(self):
        client = _client()
        _isupport(client, "LINELEN=0")
        assert client.line_len == 512

    def test_other_tokens_do_not_disturb_line_len(self):
        client = _client()
        _isupport(client, "UTF8ONLY", "NICKLEN=30")
        assert client.line_len == 512
        assert client.has_utf8only is True
