"""Unit tests for IrcPuppetPool with a fake IRC connector."""

import asyncio

import pytest

from j2i.config import BridgeMapping, Config, IRCConfig, Settings, XMPPConfig
from j2i.irc.client import IRCClient
from j2i.irc.nicks import casemap_nick, collision_puppet_nick, puppet_identity
from j2i.irc.puppets import IrcPuppetPool


class FakeClient:
    def __init__(self, nick: str, *, reject: bool = False) -> None:
        self.nick = nick
        self.nick_rejected = reject
        self.is_puppet = True
        self.has_echo_message = True
        self.has_message_tags = True
        self.nick_len = 30
        self.casemapping = "ascii"
        self.channels: dict[str, bool] = {}
        self.joined: list[str] = []
        self.parted: list[str] = []
        self.part_reasons: list[str | None] = []
        self.messages: list[tuple] = []
        self.away: list[str | None] = []
        self.setnames: list[str] = []
        self.quit_reason: str | None = None
        self.nicks: list[str] = []
        self.on_disconnect = None
        self.on_self_kicked = None
        self.on_self_message = None

    async def join(self, channel: str) -> None:
        self.joined.append(channel)
        self.channels[channel.lower()] = False

    async def part(self, channel: str, reason: str | None = None) -> None:
        self.parted.append(channel)
        self.part_reasons.append(reason)
        self.channels.pop(channel.lower(), None)

    async def send_message(self, channel, text, reply_to=None) -> None:
        self.messages.append((channel, text, reply_to))

    async def send_action(self, channel, text, reply_to=None) -> None:
        self.messages.append((channel, f"ACTION {text}", reply_to))

    async def send_away(self, reason: str | None) -> None:
        self.away.append(reason)

    async def send_setname(self, realname: str) -> bool:
        self.setnames.append(realname)
        return True

    async def change_nick(self, new_nick: str, timeout: float = 10.0) -> bool:
        self.nicks.append(new_nick)
        self.nick = new_nick
        return True

    async def send_typing(self, channel, active) -> None:
        return None

    async def send_reaction(self, channel, emoji, reply_to, *, unreact=False) -> bool:
        return True

    def can_multiline(self, lines) -> bool:
        return False

    async def disconnect(self, reason: str = "") -> None:
        self.quit_reason = reason


def _config(**irc_kwargs) -> Config:
    irc = IRCConfig(name="net", host="irc.example.org", nick="bridge", **irc_kwargs)
    return Config(
        xmpp=[XMPPConfig(name="x", jid="bot@example.com", password="p")],
        irc=[irc],
        bridges=[
            BridgeMapping(
                xmpp="x", xmpp_muc="r@c", irc="net", irc_channel="#chan",
            )
        ],
        settings=Settings(),
    )


def _master() -> IRCClient:
    master = IRCClient(host="h", port=6697, nick="bridge")
    master.channels["#chan"] = True
    return master


def _pool(cfg: Config, master: IRCClient, connector) -> IrcPuppetPool:
    return IrcPuppetPool(
        cfg, {"net": master}, connector=connector, spawn_rate=0,
    )


@pytest.mark.asyncio
async def test_acquire_uses_preferred_nick_and_owns_it():
    spawned: list[str] = []

    async def connector(irc_cfg, nick):
        spawned.append(nick)
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    assert client is not None
    assert client.nick == "alice|xmpp"
    assert spawned == ["alice|xmpp"]
    assert pool.is_owned("net", "alice|xmpp")
    assert pool.is_owned("net", "Alice|xmpp")

    again = await pool.acquire("net", "alice", "#chan")
    assert again is client
    assert spawned == ["alice|xmpp"]


@pytest.mark.asyncio
async def test_echo_handler_factory_is_bound_to_the_puppet_client():
    bound: list[object] = []

    def factory(irc_name, client):
        bound.append((irc_name, client))

        async def handler(channel, msgid):
            return None

        return handler

    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = IrcPuppetPool(
        _config(puppet_mode="nicks"),
        {"net": _master()},
        connector=connector,
        on_self_message=factory,
    )
    client = await pool.acquire("net", "alice", "#chan")
    assert bound == [("net", client)]
    assert client.on_self_message is not None


@pytest.mark.asyncio
async def test_second_channel_joins_existing_socket():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    await pool.acquire("net", "alice", "#other")
    assert client.joined == ["#chan", "#other"]


@pytest.mark.asyncio
async def test_cap_evicts_lru():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    cfg = _config(puppet_mode="nicks", max_puppets=1)
    pool = _pool(cfg, _master(), connector)
    alice = await pool.acquire("net", "alice", "#chan")
    bob = await pool.acquire("net", "bob", "#chan")
    assert bob is not None
    assert bob.nick == "bob|xmpp"
    assert alice.quit_reason == "Pool full"
    assert not pool.is_owned("net", "alice|xmpp")


@pytest.mark.asyncio
async def test_hash_fallback_when_preferred_taken():
    master = _master()
    master._channel_members["#chan"] = {"alice|xmpp"}

    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), master, connector)
    client = await pool.acquire("net", "alice", "#chan")
    assert client is not None
    expected = collision_puppet_nick(
        "alice", puppet_identity("net", "alice"), 30, "|",
    )
    assert client.nick == expected
    assert pool.is_owned("net", expected)


@pytest.mark.asyncio
async def test_single_flight_does_not_double_spawn():
    started = asyncio.Event()
    release = asyncio.Event()
    spawned = 0

    async def connector(irc_cfg, nick):
        nonlocal spawned
        spawned += 1
        started.set()
        await release.wait()
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    t1 = asyncio.create_task(pool.acquire("net", "alice", "#chan"))
    await started.wait()
    t2 = asyncio.create_task(pool.acquire("net", "alice", "#chan"))
    release.set()
    a, b = await asyncio.gather(t1, t2)
    assert a is b
    assert spawned == 1


@pytest.mark.asyncio
async def test_shutdown_quits_all():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    await pool.shutdown()
    assert client.quit_reason == "Bridge shutting down"
    assert not pool.is_owned("net", "alice|xmpp")


@pytest.mark.asyncio
async def test_connector_nick_in_use_retries_hash():
    calls: list[str] = []

    async def connector(irc_cfg, nick):
        calls.append(nick)
        if nick == "alice|xmpp":
            return FakeClient(nick, reject=True)
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    assert client is not None
    assert calls[0] == "alice|xmpp"
    assert client.nick != "alice|xmpp"
    assert casemap_nick(client.nick) != casemap_nick("alice|xmpp")


@pytest.mark.asyncio
async def test_release_parts_one_channel_and_quits_the_last():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    await pool.acquire("net", "alice", "#other")
    await pool.release("net", "alice", "#chan")
    assert client.parted == ["#chan"]
    assert client.quit_reason is None
    assert pool.get_connected("net", "alice") is client
    await pool.release("net", "alice", "#other")
    assert client.quit_reason == "Left channel"
    assert pool.get_connected("net", "alice") is None


@pytest.mark.asyncio
async def test_release_quit_reason_on_last_channel():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    await pool.release("net", "alice", "#chan", quit_reason="Kicked from XMPP MUC")
    assert client.parted == []
    assert client.quit_reason == "Kicked from XMPP MUC"
    assert pool.get_connected("net", "alice") is None


@pytest.mark.asyncio
async def test_release_quit_reason_parts_when_other_channels_remain():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    await pool.acquire("net", "alice", "#other")
    await pool.release("net", "alice", "#chan", quit_reason="Kicked from XMPP MUC")
    assert client.parted == ["#chan"]
    assert client.part_reasons == ["Kicked from XMPP MUC"]
    assert client.quit_reason is None
    assert pool.get_connected("net", "alice") is client


@pytest.mark.asyncio
async def test_rename_rekeys_and_changes_irc_nick():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    await pool.rename("net", "alice", "alison")
    assert client.nick == "alison|xmpp"
    assert client.setnames == ["alison"]
    assert pool.get_connected("net", "alice") is None
    assert pool.get_connected("net", "alison") is client
    assert pool.is_owned("net", "alison|xmpp")
    assert not pool.is_owned("net", "alice|xmpp")


@pytest.mark.asyncio
async def test_eager_does_not_lru_evict():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    cfg = _config(puppet_mode="nicks", max_puppets=1, puppet_presence="eager")
    pool = _pool(cfg, _master(), connector)
    alice = await pool.acquire("net", "alice", "#chan")
    bob = await pool.acquire("net", "bob", "#chan")
    assert bob is None
    assert alice.quit_reason is None
    assert pool.get_connected("net", "alice") is alice


@pytest.mark.asyncio
async def test_eager_skips_idle_timer():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    cfg = _config(
        puppet_mode="nicks", puppet_presence="eager", puppet_idle_seconds=1,
    )
    pool = _pool(cfg, _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    entry = pool._entries[("net", "alice")]
    assert entry.idle_handle is None
    assert client.quit_reason is None


@pytest.mark.asyncio
async def test_kick_marks_channel_and_eager_acquire_refuses():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    cfg = _config(puppet_mode="nicks", puppet_presence="eager")
    pool = _pool(cfg, _master(), connector)
    client = await pool.acquire("net", "alice", "#chan")
    entry = pool._entries[("net", "alice")]
    await pool._on_kicked(entry, "#chan")
    assert pool.was_kicked("net", "alice", "#chan")
    assert client.quit_reason == "kicked"
    refused = await pool.acquire("net", "alice", "#chan")
    assert refused is None
    again = await pool.acquire("net", "alice", "#chan", ignore_kick=True)
    assert again is not None


@pytest.mark.asyncio
async def test_set_away_and_touch_noop_when_disconnected():
    async def connector(irc_cfg, nick):
        return FakeClient(nick)

    pool = _pool(_config(puppet_mode="nicks"), _master(), connector)
    await pool.set_away("net", "alice", "brb")
    pool.touch("net", "alice")
    client = await pool.acquire("net", "alice", "#chan")
    await pool.set_away("net", "alice", "brb")
    assert client.away == ["brb"]
    pool.touch("net", "alice")

