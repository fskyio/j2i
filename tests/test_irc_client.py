"""Unit tests for IRCClient ISUPPORT parsing relevant to line-length handling."""

import asyncio
import logging

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
        assert client.nick_len == 30


class TestNickLen:
    def test_defaults_to_30(self):
        assert _client().nick_len == 30

    def test_nicklen_token_overrides_default(self):
        client = _client()
        _isupport(client, "NICKLEN=16")
        assert client.nick_len == 16

    def test_casemapping_recorded(self):
        client = _client()
        _isupport(client, "CASEMAPPING=rfc1459")
        assert client.casemapping == "rfc1459"


class TestSendReaction:
    def test_returns_false_without_message_tags(self):
        client = _client()
        assert asyncio.run(
            client.send_reaction("#c", "👍", "mid")
        ) is False

    def test_formats_tagmsg(self):
        client = _client()
        client.has_message_tags = True
        sent: list[str] = []

        async def capture(line: str) -> None:
            sent.append(line)

        client._send = capture  # type: ignore[method-assign]
        assert asyncio.run(client.send_reaction("#c", "👍", "abc")) is True
        assert sent == ["@+draft/react=👍;+reply=abc TAGMSG #c"]
        sent.clear()
        assert asyncio.run(
            client.send_reaction("#c", "👍", "abc", unreact=True)
        ) is True
        assert sent == ["@+draft/unreact=👍;+reply=abc TAGMSG #c"]


class TestBanAndKick:
    def test_mode_then_kick(self):
        client = _client()
        sent: list[str] = []

        async def fake_send(line: str) -> None:
            sent.append(line)

        client._send = fake_send  # type: ignore[method-assign]
        asyncio.run(client.ban_and_kick("#general", "alice", "Banned from XMPP MUC"))
        assert sent == [
            "MODE #general +b alice!*@*",
            "KICK #general alice :Banned from XMPP MUC",
        ]

    def test_default_reason_when_none(self):
        client = _client()
        sent: list[str] = []

        async def fake_send(line: str) -> None:
            sent.append(line)

        client._send = fake_send  # type: ignore[method-assign]
        asyncio.run(client.ban_and_kick("#c", "bob", None))
        assert sent[1] == "KICK #c bob :Banned from XMPP MUC"


class TestSendAway:
    def test_sets_reason(self):
        client = _client()
        sent: list[str] = []

        async def fake_send(line: str) -> None:
            sent.append(line)

        client._send = fake_send  # type: ignore[method-assign]
        asyncio.run(client.send_away("brb"))
        assert sent == ["AWAY :brb"]

    def test_clears_when_none_or_empty(self):
        client = _client()
        sent: list[str] = []

        async def fake_send(line: str) -> None:
            sent.append(line)

        client._send = fake_send  # type: ignore[method-assign]
        asyncio.run(client.send_away(None))
        assert sent == ["AWAY"]
        sent.clear()
        asyncio.run(client.send_away(""))
        assert sent == ["AWAY :"]


class TestChangeNick:
    def test_rejected_before_registration(self):
        client = _client()
        sent: list[str] = []

        async def fake_send(line: str) -> None:
            sent.append(line)

        client._send = fake_send  # type: ignore[method-assign]
        assert asyncio.run(client.change_nick("other")) is False
        assert sent == []

    def test_noop_when_unchanged(self):
        client = _client()
        client._registered = True
        sent: list[str] = []

        async def fake_send(line: str) -> None:
            sent.append(line)

        client._send = fake_send  # type: ignore[method-assign]
        assert asyncio.run(client.change_nick("bridge")) is True
        assert sent == []

    def test_succeeds_on_nick_echo(self):
        client = _client()
        client._registered = True

        async def fake_send(line: str) -> None:
            if line == "NICK other":
                await client._dispatch(
                    {}, "bridge!u@h", "NICK", ["other"]
                )

        client._send = fake_send  # type: ignore[method-assign]
        assert asyncio.run(client.change_nick("other")) is True
        assert client.nick == "other"

    def test_post_register_433_does_not_fail_registration(self):
        client = _client()
        client._registered = True
        client._register_event.set()

        async def fake_send(line: str) -> None:
            if line == "NICK taken":
                await client._dispatch(
                    {}, "irc.example.org", "433",
                    ["bridge", "taken", "Nickname is already in use"],
                )

        client._send = fake_send  # type: ignore[method-assign]
        assert asyncio.run(client.change_nick("taken")) is False
        assert client.nick_rejected is False
        assert client._register_event.is_set()


class TestChanopErrors:
    def test_logs_482(self, caplog):
        client = _client()
        with caplog.at_level(logging.WARNING, logger="j2i.irc.client"):
            asyncio.run(
                client._dispatch(
                    {},
                    "irc.example.org",
                    "482",
                    ["bridge", "#c", "You're not a channel operator"],
                )
            )
        assert "482" in caplog.text
        assert "not channel operator" in caplog.text
        assert "#c" in caplog.text
