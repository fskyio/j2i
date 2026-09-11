from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from j2i.config import (
    Config,
    IRCConfig,
    resolve_max_puppets,
    resolve_puppet_idle_seconds,
    resolve_puppet_presence,
    resolve_puppet_separator,
    resolve_puppet_suffix,
)
from j2i.irc.client import IRCClient, SelfMsgCallback
from j2i.irc.nicks import (
    DEFAULT_NICK_LEN,
    DEFAULT_USER_LEN,
    allocate_puppet_nick,
    casemap_nick,
    collision_puppet_nick,
    preferred_puppet_nick,
    puppet_identity,
    sanitize_irc_ident,
    sanitize_irc_realname,
)

log = logging.getLogger(__name__)

SelfMsgFactory = Callable[[str, IRCClient], SelfMsgCallback]
Connector = Callable[[IRCConfig, str], Awaitable[IRCClient]]
# irc_name, xmpp_nick, channels the socket was in
DroppedCallback = Callable[[str, str, list[str]], Awaitable[None]]

_SPAWN_RATE = 2.0  # new TCP connections per second per network
_SPAWN_BURST = 2


class NickInUse(Exception):
    """Registration failed because the nick was rejected."""


@dataclass
class _PuppetEntry:
    irc_name: str
    xmpp_nick: str
    ready: asyncio.Future
    client: IRCClient | None = None
    irc_nick: str = ""
    channels: set[str] = field(default_factory=set)
    last_used: float = 0.0
    idle_handle: asyncio.TimerHandle | None = None


class IrcPuppetPool:
    """Lazy extra IRC connections, one per XMPP nick per network."""

    def __init__(
        self,
        config: Config,
        masters: dict[str, IRCClient],
        *,
        on_self_message: SelfMsgFactory | None = None,
        on_dropped: DroppedCallback | None = None,
        connector: Connector | None = None,
        connect_timeout: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        spawn_rate: float = _SPAWN_RATE,
        spawn_burst: int = _SPAWN_BURST,
    ) -> None:
        self.config = config
        self._masters = masters
        self._on_self_message = on_self_message
        self._on_dropped = on_dropped
        self._connector = connector
        self.connect_timeout = connect_timeout
        self._clock = clock
        self._spawn_rate = spawn_rate
        self._spawn_burst = spawn_burst
        self._entries: dict[tuple[str, str], _PuppetEntry] = {}
        # irc_name -> casemapped nicks we currently own or are registering
        self._owned: dict[str, set[str]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._stopping = False
        self._run_tasks: list[asyncio.Task] = []
        # (irc_name, xmpp_nick, channel.lower()) kicked by IRC; no eager rejoin
        self._kicked: set[tuple[str, str, str]] = set()
        self._spawn_tokens: dict[str, float] = {}
        self._spawn_updated: dict[str, float] = {}
        self._spawn_locks: dict[str, asyncio.Lock] = {}

    def is_owned(self, irc_name: str, nick: str) -> bool:
        mapped = casemap_nick(nick, self._mapping(irc_name))
        return mapped in self._owned.get(irc_name, set())

    def owned_nicks(self, irc_name: str) -> set[str]:
        return set(self._owned.get(irc_name, set()))

    def get_connected(self, irc_name: str, xmpp_nick: str) -> IRCClient | None:
        entry = self._entries.get((irc_name, xmpp_nick))
        if entry is None or entry.client is None:
            return None
        return entry.client

    def display_nick(self, irc_name: str, xmpp_nick: str) -> str:
        """IRC nick to mention for this XMPP occupant (connected or preferred)."""
        entry = self._entries.get((irc_name, xmpp_nick))
        if entry is not None and entry.irc_nick:
            return entry.irc_nick
        irc_cfg = self.config.irc_by_name(irc_name)
        master = self._masters.get(irc_name)
        nicklen = master.nick_len if master is not None else DEFAULT_NICK_LEN
        return preferred_puppet_nick(
            xmpp_nick,
            resolve_puppet_suffix(irc_cfg, self.config.settings),
            resolve_puppet_separator(irc_cfg, self.config.settings),
            nicklen,
        )

    async def acquire(
        self,
        irc_name: str,
        xmpp_nick: str,
        channel: str,
        *,
        ignore_kick: bool = False,
    ) -> IRCClient | None:
        if self._stopping:
            return None
        ch = channel.lower()
        if ignore_kick:
            self.clear_kicked(irc_name, xmpp_nick, ch)
        elif self.was_kicked(irc_name, xmpp_nick, ch):
            return None
        key = (irc_name, xmpp_nick)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._acquire_locked(irc_name, xmpp_nick, channel)

    def was_kicked(self, irc_name: str, xmpp_nick: str, channel: str) -> bool:
        return (irc_name, xmpp_nick, channel.lower()) in self._kicked

    def clear_kicked(
        self, irc_name: str, xmpp_nick: str, channel: str | None = None
    ) -> None:
        if channel is None:
            self._kicked = {
                k for k in self._kicked if k[:2] != (irc_name, xmpp_nick)
            }
            return
        self._kicked.discard((irc_name, xmpp_nick, channel.lower()))

    def touch(self, irc_name: str, xmpp_nick: str) -> None:
        """Reset idle for a connected puppet (e.g. XMPP typing). No-op otherwise."""
        entry = self._entries.get((irc_name, xmpp_nick))
        if entry is None or entry.client is None:
            return
        self._touch(entry)

    async def set_away(
        self, irc_name: str, xmpp_nick: str, reason: str | None
    ) -> None:
        client = self.get_connected(irc_name, xmpp_nick)
        if client is None:
            return
        await client.send_away(reason)

    async def release(
        self, irc_name: str, xmpp_nick: str, channel: str,
        *,
        quit_reason: str | None = None,
    ) -> None:
        """PART ``channel``; QUIT the socket if it is in no channels afterwards.

        ``quit_reason`` is used for MUC kick/ban: QUIT that reason when this
        is the last channel, otherwise PART with the reason so other rooms
        keep the nick.
        """
        self.clear_kicked(irc_name, xmpp_nick, channel)
        key = (irc_name, xmpp_nick)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            ch = channel.lower()
            if quit_reason and ch in entry.channels and len(entry.channels) <= 1:
                await self._drop(entry, quit_reason)
                return
            if ch in entry.channels and entry.client is not None:
                try:
                    await entry.client.part(channel, quit_reason)
                except Exception as e:
                    log.debug(
                        "Error PARTing puppet %s from %s: %s",
                        entry.irc_nick, channel, e,
                    )
            entry.channels.discard(ch)
            if not entry.channels:
                await self._drop(entry, "Left channel")

    async def rename(
        self, irc_name: str, old_nick: str, new_nick: str
    ) -> None:
        """Rekey a puppet to a new XMPP nick and NICK the IRC socket if connected."""
        if old_nick == new_nick:
            return
        old_key = (irc_name, old_nick)
        new_key = (irc_name, new_nick)
        lock = self._locks.setdefault(old_key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(old_key)
            if entry is None:
                return
            existing = self._entries.get(new_key)
            if existing is not None and existing is not entry:
                await self._drop(existing, "Nick taken by rename")
            if entry.client is not None:
                await self._rename_irc_nick(entry, new_nick)
            kicked = [
                (n, x, ch) for n, x, ch in self._kicked
                if n == irc_name and x == old_nick
            ]
            for item in kicked:
                self._kicked.discard(item)
                self._kicked.add((irc_name, new_nick, item[2]))
            entry.xmpp_nick = new_nick
            self._entries.pop(old_key, None)
            self._entries[new_key] = entry
            new_lock = self._locks.setdefault(new_key, asyncio.Lock())
            if new_lock is not lock:
                self._locks[new_key] = lock
            self._locks.pop(old_key, None)

    async def _acquire_locked(
        self, irc_name: str, xmpp_nick: str, channel: str
    ) -> IRCClient | None:
        key = (irc_name, xmpp_nick)
        existing = self._entries.get(key)
        if existing is not None:
            client = existing.client
            if client is None and not existing.ready.done():
                client = await existing.ready
            if client is None:
                return None
            await self._ensure_joined(existing, channel)
            self._touch(existing)
            return client

        if not await self._ensure_capacity(irc_name):
            log.info(
                "Nick puppet pool full for %s, not spawning %s",
                irc_name, xmpp_nick,
            )
            return None

        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        entry = _PuppetEntry(
            irc_name=irc_name, xmpp_nick=xmpp_nick, ready=ready,
        )
        self._entries[key] = entry
        try:
            client = await asyncio.wait_for(
                self._spawn(entry, channel),
                timeout=self.connect_timeout,
            )
        except Exception as e:
            log.warning(
                "Failed to spawn IRC puppet %s on %s: %s",
                xmpp_nick, irc_name, e,
            )
            self._forget(entry)
            if not ready.done():
                ready.set_result(None)
            return None
        entry.client = client
        if not ready.done():
            ready.set_result(client)
        self._touch(entry)
        return client

    async def shutdown(self) -> None:
        self._stopping = True
        for entry in list(self._entries.values()):
            await self._drop(entry, "Bridge shutting down")
        for task in self._run_tasks:
            if not task.done():
                task.cancel()
        self._run_tasks.clear()

    def _mapping(self, irc_name: str) -> str:
        master = self._masters.get(irc_name)
        return master.casemapping if master is not None else "ascii"

    def _is_eager(self, irc_name: str) -> bool:
        try:
            irc_cfg = self.config.irc_by_name(irc_name)
        except KeyError:
            return False
        return resolve_puppet_presence(irc_cfg, self.config.settings) == "eager"

    async def _rate_limit_spawn(self, irc_name: str) -> None:
        if self._spawn_rate <= 0:
            return
        lock = self._spawn_locks.setdefault(irc_name, asyncio.Lock())
        async with lock:
            now = self._clock()
            tokens = self._spawn_tokens.get(irc_name, float(self._spawn_burst))
            last = self._spawn_updated.get(irc_name, now)
            tokens = min(
                float(self._spawn_burst),
                tokens + (now - last) * self._spawn_rate,
            )
            if tokens < 1:
                wait = (1.0 - tokens) / self._spawn_rate
                await asyncio.sleep(wait)
                now = self._clock()
                tokens = 1.0
            self._spawn_tokens[irc_name] = tokens - 1.0
            self._spawn_updated[irc_name] = now

    async def _rename_irc_nick(
        self, entry: _PuppetEntry, new_xmpp_nick: str
    ) -> None:
        client = entry.client
        if client is None:
            return
        irc_cfg = self.config.irc_by_name(entry.irc_name)
        master = self._masters.get(entry.irc_name)
        nicklen = master.nick_len if master is not None else DEFAULT_NICK_LEN
        suffix = resolve_puppet_suffix(irc_cfg, self.config.settings)
        separator = resolve_puppet_separator(irc_cfg, self.config.settings)
        identity = puppet_identity(entry.irc_name, new_xmpp_nick)
        mapping = self._mapping(entry.irc_name)
        old_irc = entry.irc_nick
        self._release_nick(entry.irc_name, old_irc)
        taken = self._taken(entry.irc_name)
        nick = allocate_puppet_nick(
            new_xmpp_nick,
            suffix=suffix,
            separator=separator,
            nicklen=nicklen,
            taken=taken,
            identity=identity,
            mapping=mapping,
        )
        if nick is None:
            self._reserve(entry.irc_name, old_irc)
            await client.send_setname(sanitize_irc_realname(new_xmpp_nick))
            return
        self._reserve(entry.irc_name, nick)
        ok = await client.change_nick(nick)
        if ok:
            entry.irc_nick = client.nick
            if casemap_nick(client.nick, mapping) != casemap_nick(nick, mapping):
                self._release_nick(entry.irc_name, nick)
                self._reserve(entry.irc_name, client.nick)
            await client.send_setname(sanitize_irc_realname(new_xmpp_nick))
            return
        self._release_nick(entry.irc_name, nick)
        fallback = collision_puppet_nick(
            new_xmpp_nick, identity, nicklen, separator,
        )
        if (
            casemap_nick(fallback, mapping) in self._taken(entry.irc_name)
            or casemap_nick(fallback, mapping) == casemap_nick(nick, mapping)
        ):
            self._reserve(entry.irc_name, old_irc)
            await client.send_setname(sanitize_irc_realname(new_xmpp_nick))
            return
        self._reserve(entry.irc_name, fallback)
        ok = await client.change_nick(fallback)
        if ok:
            entry.irc_nick = client.nick
            await client.send_setname(sanitize_irc_realname(new_xmpp_nick))
            return
        self._release_nick(entry.irc_name, fallback)
        self._reserve(entry.irc_name, old_irc)
        await client.send_setname(sanitize_irc_realname(new_xmpp_nick))

    def _reserve(self, irc_name: str, irc_nick: str) -> None:
        mapped = casemap_nick(irc_nick, self._mapping(irc_name))
        self._owned.setdefault(irc_name, set()).add(mapped)

    def _release_nick(self, irc_name: str, irc_nick: str) -> None:
        if not irc_nick:
            return
        mapped = casemap_nick(irc_nick, self._mapping(irc_name))
        owned = self._owned.get(irc_name)
        if owned is not None:
            owned.discard(mapped)

    def _taken(self, irc_name: str) -> set[str]:
        mapping = self._mapping(irc_name)
        taken = set(self._owned.get(irc_name, set()))
        master = self._masters.get(irc_name)
        if master is not None:
            taken.add(casemap_nick(master.nick, mapping))
            for ch in master.channels:
                for n in master.get_members(ch):
                    taken.add(casemap_nick(n, mapping))
        return taken

    def _touch(self, entry: _PuppetEntry) -> None:
        entry.last_used = self._clock()
        if entry.idle_handle is not None:
            entry.idle_handle.cancel()
            entry.idle_handle = None
        try:
            irc_cfg = self.config.irc_by_name(entry.irc_name)
        except KeyError:
            return
        if self._is_eager(entry.irc_name):
            return
        idle = resolve_puppet_idle_seconds(irc_cfg, self.config.settings)
        if idle <= 0:
            return
        loop = asyncio.get_running_loop()
        entry.idle_handle = loop.call_later(
            idle, lambda: asyncio.create_task(self._idle_quit(entry))
        )

    async def _idle_quit(self, entry: _PuppetEntry) -> None:
        current = self._entries.get((entry.irc_name, entry.xmpp_nick))
        if current is not entry or self._stopping:
            return
        log.info(
            "Idle-QUIT IRC puppet %s (%s) on %s",
            entry.irc_nick, entry.xmpp_nick, entry.irc_name,
        )
        await self._drop(entry, "Idle timeout")

    async def _ensure_capacity(self, irc_name: str) -> bool:
        irc_cfg = self.config.irc_by_name(irc_name)
        cap = resolve_max_puppets(irc_cfg, self.config.settings)
        if cap == 0:
            return True
        live = [
            e for e in self._entries.values()
            if e.irc_name == irc_name
        ]
        if len(live) < cap:
            return True
        if self._is_eager(irc_name):
            return False
        evictable = [e for e in live if e.client is not None]
        if not evictable:
            return False
        oldest = min(evictable, key=lambda e: e.last_used)
        await self._drop(oldest, "Pool full")
        return True

    async def _spawn(self, entry: _PuppetEntry, channel: str) -> IRCClient:
        irc_cfg = self.config.irc_by_name(entry.irc_name)
        master = self._masters.get(entry.irc_name)
        nicklen = master.nick_len if master is not None else DEFAULT_NICK_LEN
        suffix = resolve_puppet_suffix(irc_cfg, self.config.settings)
        separator = resolve_puppet_separator(irc_cfg, self.config.settings)
        identity = puppet_identity(entry.irc_name, entry.xmpp_nick)
        mapping = self._mapping(entry.irc_name)
        taken = self._taken(entry.irc_name)

        nick = allocate_puppet_nick(
            entry.xmpp_nick,
            suffix=suffix,
            separator=separator,
            nicklen=nicklen,
            taken=taken,
            identity=identity,
            mapping=mapping,
        )
        if nick is None:
            raise RuntimeError("no free puppet nick")

        await self._rate_limit_spawn(entry.irc_name)
        self._reserve(entry.irc_name, nick)
        entry.irc_nick = nick
        try:
            client = await self._connect_client(irc_cfg, nick, entry.xmpp_nick)
        except NickInUse:
            self._release_nick(entry.irc_name, nick)
            fallback = collision_puppet_nick(
                entry.xmpp_nick, identity, nicklen, separator,
            )
            if (
                casemap_nick(fallback, mapping) in self._taken(entry.irc_name)
                or casemap_nick(fallback, mapping) == casemap_nick(nick, mapping)
            ):
                raise
            self._reserve(entry.irc_name, fallback)
            entry.irc_nick = fallback
            client = await self._connect_client(irc_cfg, fallback, entry.xmpp_nick)

        client.on_disconnect = lambda: self._on_unexpected_disconnect(entry)
        client.on_self_kicked = lambda ch: self._on_kicked(entry, ch)
        if self._on_self_message is not None:
            client.on_self_message = self._on_self_message(entry.irc_name, client)
        if client.nick != entry.irc_nick:
            self._release_nick(entry.irc_name, entry.irc_nick)
            entry.irc_nick = client.nick
            self._reserve(entry.irc_name, client.nick)
        entry.client = client
        await self._ensure_joined(entry, channel)
        log.info(
            "IRC puppet %s on %s registered as %s",
            entry.xmpp_nick, entry.irc_name, client.nick,
        )
        return client

    async def _connect_client(
        self, irc_cfg: IRCConfig, nick: str, xmpp_nick: str,
    ) -> IRCClient:
        if self._connector is not None:
            client = await self._connector(irc_cfg, nick)
            if getattr(client, "nick_rejected", False):
                raise NickInUse(nick)
            return client
        master = self._masters.get(irc_cfg.name)
        userlen = master.user_len if master is not None else DEFAULT_USER_LEN
        client = IRCClient(
            host=irc_cfg.host,
            port=irc_cfg.port,
            nick=nick,
            tls=irc_cfg.tls,
            is_puppet=True,
            ident=sanitize_irc_ident(xmpp_nick, userlen),
            realname=sanitize_irc_realname(xmpp_nick),
        )
        await client.connect()
        task = asyncio.create_task(
            client.run(), name=f"irc-puppet-{irc_cfg.name}-{nick}",
        )
        self._run_tasks.append(task)
        ok = await client.wait_registered(self.connect_timeout)
        if not ok:
            rejected = client.nick_rejected
            await client.disconnect("registration failed")
            if rejected:
                raise NickInUse(nick)
            raise ConnectionError(f"puppet {nick!r} did not register")
        return client

    async def _ensure_joined(self, entry: _PuppetEntry, channel: str) -> None:
        if entry.client is None:
            return
        key = channel.lower()
        if key in entry.channels:
            return
        await entry.client.join(channel)
        entry.channels.add(key)

    async def _on_kicked(self, entry: _PuppetEntry, channel: str) -> None:
        ch = channel.lower()
        self._kicked.add((entry.irc_name, entry.xmpp_nick, ch))
        entry.channels.discard(ch)
        if not entry.channels:
            await self._drop(entry, "kicked")

    async def _on_unexpected_disconnect(self, entry: _PuppetEntry) -> None:
        log.warning(
            "IRC puppet %s on %s disconnected",
            entry.irc_nick, entry.irc_name,
        )
        channels = list(entry.channels)
        irc_name = entry.irc_name
        xmpp_nick = entry.xmpp_nick
        self._forget(entry)
        if self._on_dropped is not None and not self._stopping:
            try:
                await self._on_dropped(irc_name, xmpp_nick, channels)
            except Exception as e:
                log.debug(
                    "on_dropped for %s/%s failed: %s", irc_name, xmpp_nick, e
                )

    async def _drop(self, entry: _PuppetEntry, reason: str) -> None:
        if entry.idle_handle is not None:
            entry.idle_handle.cancel()
            entry.idle_handle = None
        client = entry.client
        entry.client = None
        if client is not None:
            try:
                await client.disconnect(reason)
            except Exception as e:
                log.debug("Error QUITting puppet %s: %s", entry.irc_nick, e)
        self._forget(entry)

    def _forget(self, entry: _PuppetEntry) -> None:
        if entry.idle_handle is not None:
            entry.idle_handle.cancel()
            entry.idle_handle = None
        self._release_nick(entry.irc_name, entry.irc_nick)
        key = (entry.irc_name, entry.xmpp_nick)
        if self._entries.get(key) is entry:
            del self._entries[key]
        if not entry.ready.done():
            entry.ready.set_result(None)
