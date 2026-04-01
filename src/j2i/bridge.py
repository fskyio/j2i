from __future__ import annotations

import asyncio
import collections
import logging
import re

from j2i.config import Config, BridgeMapping, IRCConfig
from j2i.irc.client import IRCClient, IRCMessage
from j2i.pastebin import upload as pastebin_upload
from j2i.xmpp.client import XMPPClient, XMPPMessage
from j2i.xmpp.component import XMPPComponent

log = logging.getLogger(__name__)

ANTI_PING_CHAR = "\u200b"  # zero-width space

# Max number of message ID -> nick mappings to keep per MUC
_NICK_MAP_SIZE = 500

# Reconnect backoff
_RECONNECT_BASE = 2  # seconds
_RECONNECT_MAX = 300  # 5 minutes

# Characters legal in IRC nicks (broadly permissive, covers most ircds)
_IRC_NICK_LEGAL = re.compile(r"[^a-zA-Z0-9_\-\[\]\\`^{}|()]")


def anti_ping(nick: str) -> str:
    if len(nick) < 2:
        return nick
    mid = len(nick) // 2
    return nick[:mid] + ANTI_PING_CHAR + nick[mid:]


def sanitize_irc_nick(nick: str) -> str:
    sanitized = _IRC_NICK_LEGAL.sub("-", nick)
    # Collapse consecutive dashes
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    # Strip leading/trailing dashes
    sanitized = sanitized.strip("-")
    return sanitized or "unknown"


def _puppet_jid(nick: str, irc_name: str, component_domain: str) -> str:
    """Build the puppet JID for an IRC user under a component domain."""
    localpart = f"{sanitize_irc_nick(nick).lower()}.{irc_name}"
    return f"{localpart}@{component_domain}"


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.irc_clients: dict[str, IRCClient] = {}
        self.xmpp_clients: dict[str, XMPPClient | XMPPComponent] = {}
        # Subset of xmpp_clients that are components, for puppet-specific calls
        self.xmpp_components: dict[str, XMPPComponent] = {}

        # Lookup tables: map (protocol-specific key) -> list of bridge mappings
        # IRC key: (irc_name, channel)
        # XMPP key: (xmpp_name, muc_jid)
        self._irc_to_bridges: dict[tuple[str, str], list[BridgeMapping]] = {}
        self._xmpp_to_bridges: dict[tuple[str, str], list[BridgeMapping]] = {}

        # Maps XMPP message ID -> original IRC nick, so replies to bridged
        # messages can resolve the actual IRC user instead of the bot nick.
        self._msg_id_to_irc_nick: collections.OrderedDict[str, str] = (
            collections.OrderedDict()
        )

        # Reply threading (IRC msgid → XMPP stanza-id)
        # Temp: XMPP client_id → IRC msgid (cleared when stanza-id arrives)
        self._xmpp_cid_to_irc_msgid: dict[str, str] = {}
        # Final: IRC msgid → XMPP stanza-id (used when IRC user replies)
        self._irc_msgid_to_xmpp_sid: collections.OrderedDict[str, str] = (
            collections.OrderedDict()
        )
        # Reverse: XMPP stanza-id → IRC msgid (for XMPP→IRC native reply tags)
        self._xmpp_sid_to_irc_msgid: collections.OrderedDict[str, str] = (
            collections.OrderedDict()
        )
        # Pending XMPP stanza-ids awaiting IRC echo-message, per (irc_name, channel)
        self._pending_echo_sids: dict[
            tuple[str, str], collections.deque[str]
        ] = {}

        # Sender MUC occupant JID tracking for XEP-0461 reply "to" attribute
        # Temp: XMPP client_id → sender MUC occupant JID (cleared when stanza-id arrives)
        self._xmpp_cid_to_sender_jid: dict[str, str] = {}
        # Final: XMPP stanza-id → sender MUC occupant JID
        self._xmpp_sid_to_sender_jid: collections.OrderedDict[str, str] = (
            collections.OrderedDict()
        )

    def _setting(self, b: BridgeMapping, name: str):
        """Resolve a setting: per-bridge override if set, else global."""
        val = getattr(b, name, None)
        if val is not None:
            return val
        return getattr(self.config.settings, name)

    async def start(self) -> None:
        self._build_clients()
        self._build_lookup_tables()
        self._wire_callbacks()
        await self._connect_all()

    def _build_clients(self) -> None:
        for xmpp_cfg in self.config.xmpp:
            if xmpp_cfg.component:
                client: XMPPClient | XMPPComponent = XMPPComponent(
                    component_domain=xmpp_cfg.component_domain,  # type: ignore[arg-type]
                    password=xmpp_cfg.password,
                    component_host=xmpp_cfg.component_host,
                    component_port=xmpp_cfg.component_port,
                    nick=xmpp_cfg.nick,
                )
                self.xmpp_components[xmpp_cfg.name] = client  # type: ignore[assignment]
            else:
                client = XMPPClient(
                    jid=xmpp_cfg.jid,
                    password=xmpp_cfg.password,
                    nick=xmpp_cfg.nick,
                )
            self.xmpp_clients[xmpp_cfg.name] = client

        for irc_cfg in self.config.irc:
            client = IRCClient(
                host=irc_cfg.host,
                port=irc_cfg.port,
                nick=irc_cfg.nick,
                tls=irc_cfg.tls,
                sasl_password=irc_cfg.sasl_password,
                nickserv_password=irc_cfg.nickserv_password,
            )
            self.irc_clients[irc_cfg.name] = client

    def _build_lookup_tables(self) -> None:
        for b in self.config.bridges:
            irc_key = (b.irc, b.irc_channel.lower())
            xmpp_key = (b.xmpp, b.xmpp_muc.lower())
            self._irc_to_bridges.setdefault(irc_key, []).append(b)
            self._xmpp_to_bridges.setdefault(xmpp_key, []).append(b)

            # Register MUC on the XMPP client
            self.xmpp_clients[b.xmpp].add_muc(b.xmpp_muc)

    def _wire_callbacks(self) -> None:
        for irc_name, client in self.irc_clients.items():
            client.on_message = self._make_irc_message_handler(irc_name)
            client.on_action = self._make_irc_action_handler(irc_name)
            client.on_typing = self._make_irc_typing_handler(irc_name)
            client.on_user_away = self._make_irc_away_handler(irc_name)
            client.on_self_kicked = self._make_irc_self_kicked_handler(irc_name, client)
            client.on_self_message = self._make_irc_self_msg_handler(irc_name)
            # Presence callbacks for component-backed bridges
            client.on_user_join = self._make_irc_join_handler(irc_name)
            client.on_user_part = self._make_irc_part_handler(irc_name)
            client.on_user_quit = self._make_irc_quit_handler(irc_name)
            client.on_user_nick = self._make_irc_nick_handler(irc_name)
            client.on_names_done = self._make_irc_names_handler(irc_name)

        for xmpp_name, client in self.xmpp_clients.items():
            client.on_message = self._make_xmpp_handler(xmpp_name)
            client.on_self_message = self._on_xmpp_self_message

        for xmpp_name, component in self.xmpp_components.items():
            component.on_reconnected = self._make_xmpp_reconnect_handler(xmpp_name)

    async def _connect_all(self) -> None:
        tasks: list[asyncio.Task] = []

        for name, client in self.xmpp_clients.items():
            log.info("Connecting XMPP: %s", name)
            tasks.append(asyncio.create_task(client.connect()))

        for name, client in self.irc_clients.items():
            log.info("Connecting IRC: %s", name)
            tasks.append(asyncio.create_task(self._connect_irc(name, client)))

        await asyncio.gather(*tasks)
        log.info("All connections established")

    async def _connect_irc(self, name: str, client: IRCClient) -> None:
        # Wire disconnect callback for reconnection
        client.on_disconnect = lambda: self._reconnect_irc(name, client)

        await self._do_connect_irc(name, client)

    async def _do_connect_irc(self, name: str, client: IRCClient) -> None:
        await client.connect()

        # Start reading in the background
        asyncio.create_task(client.run())

        # Wait for registration, then join channels
        await client._register_event.wait()

        channels = {
            b.irc_channel
            for b in self.config.bridges
            if b.irc == name
        }
        for ch in channels:
            await client.join(ch)
            log.info("Joined IRC channel: %s on %s", ch, name)

    async def _reconnect_irc(self, name: str, client: IRCClient) -> None:
        delay = _RECONNECT_BASE
        while True:
            log.info(
                "Reconnecting to IRC %s in %d seconds...", name, delay
            )
            await asyncio.sleep(delay)
            try:
                await self._do_connect_irc(name, client)
                log.info("Reconnected to IRC %s", name)
                return
            except (OSError, ConnectionRefusedError) as e:
                log.warning("IRC reconnect failed for %s: %s", name, e)
                delay = min(delay * 2, _RECONNECT_MAX)

    # ---------- IRC -> XMPP ----------

    def _record_msg_id(self, msg_id: str, irc_nick: str) -> None:
        log.debug("Recording msg_id=%s -> irc_nick=%s", msg_id, irc_nick)
        self._msg_id_to_irc_nick[msg_id] = irc_nick
        if len(self._msg_id_to_irc_nick) > _NICK_MAP_SIZE:
            self._msg_id_to_irc_nick.popitem(last=False)

    async def _on_xmpp_self_message(
        self, client_id: str, stanza_id: str
    ) -> None:
        """Re-key nick and IRC msgid mappings when we learn the server stanza-id."""
        irc_nick = self._msg_id_to_irc_nick.pop(client_id, None)
        if irc_nick:
            log.debug(
                "Re-keying %s -> %s for nick=%s",
                client_id, stanza_id, irc_nick,
            )
            self._msg_id_to_irc_nick[stanza_id] = irc_nick

        sender_jid = self._xmpp_cid_to_sender_jid.pop(client_id, None)
        if sender_jid:
            self._xmpp_sid_to_sender_jid[stanza_id] = sender_jid
            if len(self._xmpp_sid_to_sender_jid) > _NICK_MAP_SIZE:
                self._xmpp_sid_to_sender_jid.popitem(last=False)

        irc_msgid = self._xmpp_cid_to_irc_msgid.pop(client_id, None)
        if irc_msgid:
            self._irc_msgid_to_xmpp_sid[irc_msgid] = stanza_id
            if len(self._irc_msgid_to_xmpp_sid) > _NICK_MAP_SIZE:
                self._irc_msgid_to_xmpp_sid.popitem(last=False)
            self._xmpp_sid_to_irc_msgid[stanza_id] = irc_msgid
            if len(self._xmpp_sid_to_irc_msgid) > _NICK_MAP_SIZE:
                self._xmpp_sid_to_irc_msgid.popitem(last=False)

    def _strip_reply_prefix(self, text: str, xmpp_sid: str) -> str:
        """Strip the IRC client's 'nick: ' reply fallback when sending a native reply."""
        replied_nick = self._msg_id_to_irc_nick.get(xmpp_sid)
        if replied_nick and text.startswith(f"{replied_nick}: "):
            return text[len(replied_nick) + 2:]
        return text

    def _make_irc_message_handler(self, irc_name: str):
        async def handler(irc_msg: IRCMessage) -> None:
            channel, nick, text = irc_msg.channel, irc_msg.nick, irc_msg.text
            key = (irc_name, channel.lower())
            bridges = self._irc_to_bridges.get(key, [])
            for b in bridges:
                xmpp_sid = (
                    self._irc_msgid_to_xmpp_sid.get(irc_msg.reply_to_msgid)
                    if irc_msg.reply_to_msgid
                    else None
                )
                # Look up the original sender's MUC occupant JID for XEP-0461 "to"
                reply_to_sender = (
                    self._xmpp_sid_to_sender_jid.get(xmpp_sid)
                    if xmpp_sid
                    else None
                )
                # Strip redundant "nick: " fallback when sending a native reply
                relay_text = self._strip_reply_prefix(text, xmpp_sid) if xmpp_sid else text

                if b.xmpp in self.xmpp_components:
                    component = self.xmpp_components[b.xmpp]
                    xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
                    irc_cfg = self.config.irc_by_name(irc_name)
                    pjid = _puppet_jid(nick, irc_cfg.name, xmpp_cfg.component_domain)  # type: ignore[arg-type]
                    if xmpp_sid:
                        msg_id = await component.send_puppet_reply(
                            b.xmpp_muc, pjid, relay_text, xmpp_sid,
                            reply_to=reply_to_sender,
                        )
                    else:
                        msg_id = await component.send_puppet_message(b.xmpp_muc, pjid, relay_text)
                    if not msg_id:
                        continue
                    self._record_msg_id(msg_id, nick)
                    if irc_msg.msgid:
                        self._xmpp_cid_to_irc_msgid[msg_id] = irc_msg.msgid
                    # Track sender MUC JID for future replies to this message
                    actual_nick = component._puppet_nicks.get(
                        b.xmpp_muc.lower(), {}
                    ).get(pjid, nick)
                    self._xmpp_cid_to_sender_jid[msg_id] = f"{b.xmpp_muc}/{actual_nick}"
                else:
                    xmpp_client = self.xmpp_clients[b.xmpp]
                    display_nick = (
                        anti_ping(nick) if self._setting(b, "anti_ping") else nick
                    )
                    await xmpp_client.send_typing(b.xmpp_muc, False)
                    if xmpp_sid:
                        msg_id = await xmpp_client.send_reply(
                            b.xmpp_muc, f"<{display_nick}> {relay_text}", xmpp_sid,
                            reply_to=reply_to_sender,
                        )
                    else:
                        msg_id = await xmpp_client.send_message(
                            b.xmpp_muc, f"<{display_nick}> {relay_text}"
                        )
                    self._record_msg_id(msg_id, nick)
                    if irc_msg.msgid:
                        self._xmpp_cid_to_irc_msgid[msg_id] = irc_msg.msgid
                    # Track sender MUC JID for future replies to this message
                    self._xmpp_cid_to_sender_jid[msg_id] = f"{b.xmpp_muc}/{xmpp_client.nick}"

        return handler

    def _make_irc_action_handler(self, irc_name: str):
        async def handler(irc_msg: IRCMessage) -> None:
            channel, nick, text = irc_msg.channel, irc_msg.nick, irc_msg.text
            key = (irc_name, channel.lower())
            bridges = self._irc_to_bridges.get(key, [])
            for b in bridges:
                if b.xmpp in self.xmpp_components:
                    component = self.xmpp_components[b.xmpp]
                    xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
                    irc_cfg = self.config.irc_by_name(irc_name)
                    pjid = _puppet_jid(nick, irc_cfg.name, xmpp_cfg.component_domain)  # type: ignore[arg-type]
                    await component.send_puppet_action(b.xmpp_muc, pjid, text)
                else:
                    xmpp_client = self.xmpp_clients[b.xmpp]
                    display_nick = (
                        anti_ping(nick) if self._setting(b, "anti_ping") else nick
                    )
                    await xmpp_client.send_typing(b.xmpp_muc, False)
                    msg_id = await xmpp_client.send_message(
                        b.xmpp_muc, f"* {display_nick} {text}"
                    )
                    self._record_msg_id(msg_id, nick)

        return handler

    def _make_irc_typing_handler(self, irc_name: str):
        async def handler(channel: str, nick: str, is_typing: bool) -> None:
            key = (irc_name, channel.lower())
            bridges = self._irc_to_bridges.get(key, [])
            for b in bridges:
                if b.xmpp in self.xmpp_components:
                    component = self.xmpp_components[b.xmpp]
                    xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
                    irc_cfg = self.config.irc_by_name(irc_name)
                    pjid = _puppet_jid(nick, irc_cfg.name, xmpp_cfg.component_domain)  # type: ignore[arg-type]
                    await component.send_puppet_typing(b.xmpp_muc, pjid, is_typing)
                else:
                    await self.xmpp_clients[b.xmpp].send_typing(b.xmpp_muc, is_typing)

        return handler

    # ---------- IRC presence -> XMPP puppet management ----------

    def _puppet_args(
        self, b: BridgeMapping, irc_name: str, nick: str
    ) -> tuple[XMPPComponent, str, str] | None:
        """Return (component, muc_jid, puppet_jid) if this bridge uses a component, else None."""
        if b.xmpp not in self.xmpp_components:
            return None
        component = self.xmpp_components[b.xmpp]
        xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
        irc_cfg = self.config.irc_by_name(irc_name)
        pjid = _puppet_jid(nick, irc_cfg.name, xmpp_cfg.component_domain)  # type: ignore[arg-type]
        return component, b.xmpp_muc, pjid

    def _make_irc_join_handler(self, irc_name: str):
        async def handler(channel: str, nick: str) -> None:
            key = (irc_name, channel.lower())
            for b in self._irc_to_bridges.get(key, []):
                args = self._puppet_args(b, irc_name, nick)
                if args:
                    component, muc_jid, pjid = args
                    await component.join_puppet(muc_jid, pjid, nick)

        return handler

    def _make_irc_part_handler(self, irc_name: str):
        async def handler(channel: str, nick: str) -> None:
            key = (irc_name, channel.lower())
            for b in self._irc_to_bridges.get(key, []):
                args = self._puppet_args(b, irc_name, nick)
                if args:
                    component, muc_jid, pjid = args
                    await component.part_puppet(muc_jid, pjid, nick)

        return handler

    def _make_irc_quit_handler(self, irc_name: str):
        async def handler(nick: str, channels: list[str]) -> None:
            for channel in channels:
                key = (irc_name, channel.lower())
                for b in self._irc_to_bridges.get(key, []):
                    args = self._puppet_args(b, irc_name, nick)
                    if args:
                        component, muc_jid, pjid = args
                        await component.part_puppet(muc_jid, pjid, nick)

        return handler

    def _make_irc_nick_handler(self, irc_name: str):
        async def handler(old_nick: str, new_nick: str, channels: list[str]) -> None:
            for channel in channels:
                key = (irc_name, channel.lower())
                for b in self._irc_to_bridges.get(key, []):
                    if b.xmpp not in self.xmpp_components:
                        continue
                    component = self.xmpp_components[b.xmpp]
                    xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
                    irc_cfg = self.config.irc_by_name(irc_name)
                    domain = xmpp_cfg.component_domain  # type: ignore[arg-type]
                    old_pjid = _puppet_jid(old_nick, irc_cfg.name, domain)
                    new_pjid = _puppet_jid(new_nick, irc_cfg.name, domain)
                    await component.part_puppet(b.xmpp_muc, old_pjid, old_nick)
                    await component.join_puppet(b.xmpp_muc, new_pjid, new_nick)

        return handler

    def _make_xmpp_reconnect_handler(self, xmpp_name: str):
        """After XMPP component reconnects, re-request NAMES on all bridged IRC channels.

        This re-triggers on_names_done which calls join_puppet for every current
        IRC member, repopulating _puppet_nicks so IRC→XMPP bridging resumes.
        """
        async def handler() -> None:
            log.info("XMPP component %s reconnected, re-joining puppets", xmpp_name)
            for b in self.config.bridges:
                if b.xmpp != xmpp_name or b.xmpp not in self.xmpp_components:
                    continue
                irc_client = self.irc_clients.get(b.irc)
                if irc_client is None:
                    continue
                await irc_client.request_names(b.irc_channel)

        return handler

    def _make_irc_names_handler(self, irc_name: str):
        """On initial NAMES reply, eagerly join all puppets to the MUC."""
        async def handler(channel: str, nicks: set[str]) -> None:
            key = (irc_name, channel.lower())
            for b in self._irc_to_bridges.get(key, []):
                if b.xmpp not in self.xmpp_components:
                    continue
                component = self.xmpp_components[b.xmpp]
                xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
                irc_cfg = self.config.irc_by_name(irc_name)
                domain = xmpp_cfg.component_domain  # type: ignore[arg-type]
                for nick in nicks:
                    pjid = _puppet_jid(nick, irc_cfg.name, domain)
                    asyncio.create_task(
                        component.join_puppet(b.xmpp_muc, pjid, nick)
                    )

        return handler

    def _make_irc_away_handler(self, irc_name: str):
        async def handler(nick: str, channels: list[str], reason: str | None) -> None:
            for channel in channels:
                key = (irc_name, channel.lower())
                for b in self._irc_to_bridges.get(key, []):
                    args = self._puppet_args(b, irc_name, nick)
                    if args:
                        component, muc_jid, pjid = args
                        await component.set_puppet_away(muc_jid, pjid, nick, reason)

        return handler

    # ---------- XMPP -> IRC ----------

    def _make_xmpp_typing_handler(self, xmpp_name: str):
        async def handler(muc_jid: str, nick: str, is_typing: bool) -> None:
            key = (xmpp_name, muc_jid.lower())
            bridges = self._xmpp_to_bridges.get(key, [])
            for b in bridges:
                irc_client = self.irc_clients[b.irc]
                await irc_client.send_typing(b.irc_channel, is_typing)

        return handler

    def _make_xmpp_handler(self, xmpp_name: str):
        async def handler(msg: XMPPMessage) -> None:
            # Extra safety: if this nick is a puppet, don't relay back to IRC.
            # The component's _is_puppet_echo should have caught this already,
            # but guard against edge cases (timing, reconnects, etc.)
            if xmpp_name in self.xmpp_components:
                component = self.xmpp_components[xmpp_name]
                if component.is_puppet_nick(msg.muc_jid, msg.nick):
                    log.warning(
                        "Puppet message from %r in %s leaked past echo filter, dropping",
                        msg.nick, msg.muc_jid,
                    )
                    return

            key = (xmpp_name, msg.muc_jid.lower())
            bridges = self._xmpp_to_bridges.get(key, [])
            for b in bridges:
                irc_client = self.irc_clients[b.irc]
                irc_cfg = self.config.irc_by_name(b.irc)
                if msg.is_action:
                    await self._relay_action_to_irc(
                        irc_client, irc_cfg, b.irc_channel, msg, b, xmpp_name
                    )
                else:
                    await self._relay_to_irc(
                        irc_client, irc_cfg, b.irc_channel, msg, b, xmpp_name
                    )
                # Store sender MUC occupant JID for reply "to" attribute
                if msg.stanza_id:
                    self._xmpp_sid_to_sender_jid[msg.stanza_id] = f"{msg.muc_jid}/{msg.nick}"
                    if len(self._xmpp_sid_to_sender_jid) > _NICK_MAP_SIZE:
                        self._xmpp_sid_to_sender_jid.popitem(last=False)
                # Queue the XMPP stanza-id so the IRC echo-message can map it
                if msg.stanza_id and irc_client.has_echo_message:
                    echo_key = (b.irc, b.irc_channel.lower())
                    q = self._pending_echo_sids.setdefault(
                        echo_key, collections.deque(maxlen=_NICK_MAP_SIZE)
                    )
                    q.append(msg.stanza_id)

        return handler

    def _make_irc_self_msg_handler(self, irc_name: str):
        """Handle echo-message: map IRC msgid → XMPP stanza-id for reply threading."""
        async def handler(channel: str, msgid: str) -> None:
            echo_key = (irc_name, channel.lower())
            q = self._pending_echo_sids.get(echo_key)
            if q:
                xmpp_sid = q.popleft()
                self._irc_msgid_to_xmpp_sid[msgid] = xmpp_sid
                if len(self._irc_msgid_to_xmpp_sid) > _NICK_MAP_SIZE:
                    self._irc_msgid_to_xmpp_sid.popitem(last=False)
                self._xmpp_sid_to_irc_msgid[xmpp_sid] = msgid
                if len(self._xmpp_sid_to_irc_msgid) > _NICK_MAP_SIZE:
                    self._xmpp_sid_to_irc_msgid.popitem(last=False)

        return handler

    def _make_irc_self_kicked_handler(self, irc_name: str, client: IRCClient):
        async def handler(channel: str) -> None:
            log.warning("Bridge bot kicked from %s on %s, rejoining...", channel, irc_name)
            # Brief backoff before rejoin attempt
            await asyncio.sleep(3)
            try:
                await client.join(channel)
                log.info("Rejoined %s on %s after kick", channel, irc_name)
            except Exception as e:
                log.warning("Failed to rejoin %s on %s: %s", channel, irc_name, e)

        return handler

    def _format_reply_prefix(
        self,
        msg: XMPPMessage,
        irc_client: IRCClient,
        irc_cfg: IRCConfig,
        xmpp_name: str | None = None,
    ) -> str:
        """Build a reply mention prefix like 'nick: ' for smart replies."""
        if not msg.reply_to_nick:
            return ""

        # Check if the replied-to message was sent by the bridge on behalf
        # of an IRC user - if so, use the original IRC nick directly
        log.debug(
            "Reply lookup: reply_to_id=%s, known_ids=%s",
            msg.reply_to_id,
            list(self._msg_id_to_irc_nick.keys())[-5:],
        )
        if msg.reply_to_id and msg.reply_to_id in self._msg_id_to_irc_nick:
            target = self._msg_id_to_irc_nick[msg.reply_to_id]
            return f"{target}: "

        target = msg.reply_to_nick

        # In component mode the replied-to nick might be an IRC puppet -
        # those are real IRC users and should be pinged directly, not with
        # a /xmpp suffix.
        if xmpp_name in self.xmpp_components:
            component = self.xmpp_components[xmpp_name]
            if component.is_puppet_nick(msg.muc_jid, target):
                return f"{target}: "

        # Native XMPP user: if relaymsg is active they appear on IRC
        # with a /xmpp suffix
        if irc_cfg.relaymsg and irc_client.has_relaymsg:
            target = (
                sanitize_irc_nick(target)
                + irc_client.relaymsg_separator
                + "xmpp"
            )
        return f"{target}: "

    def _format_correction(self, text: str) -> str:
        """Format an edit as an asterisk correction."""
        return f"* {text}"

    async def _relay_to_irc(
        self,
        irc_client: IRCClient,
        irc_cfg: IRCConfig,
        channel: str,
        msg: XMPPMessage,
        b: BridgeMapping,
        xmpp_name: str | None = None,
    ) -> None:
        text = msg.body
        if msg.is_correction:
            text = self._format_correction(text)

        reply_prefix = self._format_reply_prefix(msg, irc_client, irc_cfg, xmpp_name)

        # Look up native IRC reply tag for XEP-0461 replies
        irc_reply_to = (
            self._xmpp_sid_to_irc_msgid.get(msg.reply_to_id)
            if msg.reply_to_id
            else None
        )

        lines = text.split("\n")
        max_lines = self._setting(b, "max_lines")
        pastebin = self._setting(b, "pastebin")

        # If message exceeds max_lines and pastebin is configured, upload it
        if max_lines > 0 and len(lines) > max_lines and pastebin:
            paste_url = await pastebin_upload(
                pastebin,
                text,
                auth=self._setting(b, "pastebin_auth"),
                field_override=self._setting(b, "pastebin_field"),
            )
            if paste_url:
                line_text = f"{reply_prefix}(long message) {paste_url}"
                await self._send_irc_line(
                    irc_client, irc_cfg, channel, msg.nick, line_text, b,
                    reply_to=irc_reply_to,
                )
                return
            # Fall through to truncated relay if upload fails

        if max_lines > 0:
            lines = lines[:max_lines]

        for i, line in enumerate(lines):
            if not line.strip():
                continue
            # Only prepend reply prefix and native reply tag to the first line
            prefix = reply_prefix if i == 0 else ""
            reply_tag = irc_reply_to if i == 0 else None
            await self._send_irc_line(
                irc_client, irc_cfg, channel, msg.nick, f"{prefix}{line}", b,
                reply_to=reply_tag,
            )

    async def _send_irc_line(
        self,
        irc_client: IRCClient,
        irc_cfg: IRCConfig,
        channel: str,
        nick: str,
        text: str,
        b: BridgeMapping,
        reply_to: str | None = None,
    ) -> None:
        if irc_cfg.relaymsg and irc_client.can_relaymsg(channel):
            await irc_client.send_relaymsg(
                channel, sanitize_irc_nick(nick), text, reply_to=reply_to
            )
        else:
            display_nick = (
                anti_ping(nick)
                if self._setting(b, "anti_ping")
                else nick
            )
            await irc_client.send_message(
                channel, f"<{display_nick}> {text}", reply_to=reply_to
            )

    async def _relay_action_to_irc(
        self,
        irc_client: IRCClient,
        irc_cfg: IRCConfig,
        channel: str,
        msg: XMPPMessage,
        b: BridgeMapping,
        xmpp_name: str | None = None,
    ) -> None:
        irc_reply_to = (
            self._xmpp_sid_to_irc_msgid.get(msg.reply_to_id)
            if msg.reply_to_id
            else None
        )
        if irc_cfg.relaymsg and irc_client.can_relaymsg(channel):
            await irc_client.send_relaymsg(
                channel,
                sanitize_irc_nick(msg.nick),
                f"\x01ACTION {msg.body}\x01",
                reply_to=irc_reply_to,
            )
        else:
            display_nick = (
                anti_ping(msg.nick)
                if self._setting(b, "anti_ping")
                else msg.nick
            )
            await irc_client.send_message(
                channel, f"* {display_nick} {msg.body}"
            )
