from __future__ import annotations

import asyncio
import collections
import logging
import re
from dataclasses import dataclass
from typing import TypeVar

from j2i.config import Config, BridgeMapping, IRCConfig, resolve_puppet_mode, resolve_puppet_presence
from j2i.irc.client import IRCClient, IRCMessage
from j2i.irc.nicks import sanitize_irc_nick
from j2i.irc.puppets import IrcPuppetPool
from j2i.pastebin import upload as pastebin_upload
from j2i.xmpp.avatar import Avatar, default_avatar_path
from j2i.xmpp.client import XMPPClient, XMPPMessage
from j2i.xmpp.component import XMPPComponent
from j2i.xmpp.presence import OccupantEvent

log = logging.getLogger(__name__)

ANTI_PING_CHAR = "\u200b"  # zero-width space

# Max number of message ID -> nick mappings to keep per MUC
_NICK_MAP_SIZE = 500

# Max characters in an inline reply excerpt shown on IRC
_REPLY_QUOTE_MAX = 60

# Max characters in a KICK reason originating from an XMPP ban
_KICK_REASON_MAX = 80

# Reserve (bytes) for the source prefix ":nick!user@host " that the server
# prepends when relaying a message to other clients. It counts toward the
# line-length limit but is not part of what we send, so we hold it back.
# Sized generously to cover a long nick + ident + host.
_SOURCE_PREFIX_RESERVE = 100

# Floor for the auto-computed per-line body budget, so pathological framing
# (e.g. a very long channel name) can never drive it to an unusable value.
_MIN_LINE_BUDGET = 40


def _split_to_byte_limit(text: str, limit: int) -> list[str]:
    """Split a single logical line into chunks of at most `limit` UTF-8 bytes.

    Breaks on spaces where possible, falling back to a hard character-boundary
    break for a single word longer than the limit. Never splits a multibyte
    character. `text` must not contain newlines.
    """
    if limit <= 0 or len(text.encode("utf-8")) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining.encode("utf-8")) > limit:
        # Largest character-aligned prefix that fits in `limit` bytes:
        # decode the first `limit` bytes and drop any trailing partial char.
        head = remaining.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
        next_char = remaining[len(head):len(head) + 1]
        if next_char in ("", " "):
            # `head` already ends on a word boundary; keep it whole.
            chunk = head
        else:
            # `head` cut a word in half; back off to the last space if any.
            cut = head.rfind(" ")
            chunk = head[:cut] if cut > 0 else head
        if not chunk:
            # A single character wider than the limit; emit it anyway so we
            # always make progress rather than looping forever.
            chunk = remaining[0]
        chunks.append(chunk)
        remaining = remaining[len(chunk):].lstrip(" ")
    if remaining:
        chunks.append(remaining)
    return chunks

# Reconnect backoff
_RECONNECT_BASE = 2  # seconds
_RECONNECT_MAX = 300  # 5 minutes


def anti_ping(nick: str) -> str:
    if len(nick) < 2:
        return nick
    mid = len(nick) // 2
    return nick[:mid] + ANTI_PING_CHAR + nick[mid:]


def format_irc_to_xmpp(text: str) -> str:
    """Convert basic IRC formatting to XMPP markdown (XEP-0393) and strip the rest."""
    text = re.sub(r'\x03(?:\d{1,2}(?:,\d{1,2})?)?', '', text)
    text = re.sub(r'\x04[0-9a-fA-F]{0,6}', '', text)
    text = re.sub(r'\x08', '', text)
    
    out = []
    bold = False
    italic = False
    strike = False
    mono = False

    def reset() -> str:
        nonlocal bold, italic, strike, mono
        res = ""
        if mono:
            res += "`"; mono = False
        if strike:
            res += "~"; strike = False
        if italic:
            res += "_"; italic = False
        if bold:
            res += "*"; bold = False
        return res

    for char in text:
        if char == '\x02':
            out.append('*')
            bold = not bold
        elif char == '\x1d':
            out.append('_')
            italic = not italic
        elif char == '\x1e':
            out.append('~')
            strike = not strike
        elif char == '\x11':
            out.append('`')
            mono = not mono
        elif char == '\x0f':
            out.append(reset())
        elif char in ('\x16', '\x1f'):
            pass
        else:
            out.append(char)
            
    out.append(reset())
    res = "".join(out)
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', res)


def _puppet_jid(nick: str, irc_name: str, component_domain: str) -> str:
    """Build the puppet JID for an IRC user under a component domain."""
    localpart = f"{sanitize_irc_nick(nick).lower()}.{irc_name}"
    return f"{localpart}@{component_domain}"


def _relaymsg_nick(nick: str, separator: str, suffix: str) -> str:
    """Apply the RELAYMSG suffix to a (sanitized) nick, e.g. 'alice/bridge'."""
    return sanitize_irc_nick(nick) + separator + suffix


def _excerpt(body: str) -> str:
    """Flatten newlines and truncate a body to a single-line quote excerpt."""
    text = body.replace("\n", " ").strip()
    if len(text) > _REPLY_QUOTE_MAX:
        text = text[: _REPLY_QUOTE_MAX - 1] + "…"
    return text


def _ban_kick_reason(reason: str | None, actor: str | None) -> str:
    """Build a short IRC KICK reason for a synced XMPP puppet ban."""
    if actor and reason:
        text = f"Banned from XMPP ({actor}): {reason}"
    elif reason:
        text = f"Banned from XMPP: {reason}"
    elif actor:
        text = f"Banned from XMPP by {actor}"
    else:
        text = "Banned from XMPP MUC"
    text = text.replace("\n", " ").strip()
    if len(text) > _KICK_REASON_MAX:
        text = text[: _KICK_REASON_MAX - 1] + "…"
    return text


def _kick_quit_reason(reason: str | None, actor: str | None) -> str:
    """Build a short IRC QUIT/PART reason for an XMPP MUC kick."""
    if actor and reason:
        text = f"Kicked from XMPP ({actor}): {reason}"
    elif reason:
        text = f"Kicked from XMPP: {reason}"
    elif actor:
        text = f"Kicked from XMPP by {actor}"
    else:
        text = "Kicked from XMPP MUC"
    text = text.replace("\n", " ").strip()
    if len(text) > _KICK_REASON_MAX:
        text = text[: _KICK_REASON_MAX - 1] + "…"
    return text


def _occupant_irc_quit_reason(event: OccupantEvent) -> str | None:
    """QUIT/PART reason when a MUC leave was a kick or ban; None if voluntary."""
    if "301" in event.status_codes:
        return _ban_kick_reason(event.reason, event.actor)
    if "307" in event.status_codes:
        return _kick_quit_reason(event.reason, event.actor)
    return None


def _build_reply_prefix(target: str, style: str, cached_body: str | None) -> str:
    """Assemble the IRC reply prefix for an already-resolved target nick.

    With style 'quote' and a cached original body, includes a truncated,
    newline-flattened excerpt: '(re nick: "excerpt…") '. Otherwise falls back
    to a bare 'nick: ' ping.
    """
    if style == "quote" and cached_body:
        return f'(re {target}: "{_excerpt(cached_body)}") '
    return f"{target}: "


def _build_reaction_text(emoji: str, style: str, cached_body: str | None) -> str:
    """Build the IRC text body for a bridged XMPP reaction.

    With style 'quote' and a cached original body, includes a truncated
    excerpt: '(reacted to "excerpt…") <emoji>'. Otherwise falls back to
    'reacted <emoji>'.
    """
    if style == "quote" and cached_body:
        return f'(reacted to "{_excerpt(cached_body)}") {emoji}'
    return f"reacted {emoji}"


def _strip_nick_prefix(text: str, nick: str | None) -> str:
    """Remove a leading 'nick: ' fallback prefix from text, if present."""
    if nick and text.startswith(f"{nick}: "):
        return text[len(nick) + 2:]
    return text


@dataclass(frozen=True)
class MsgRef:
    """A message identifier scoped to one connection and room.

    IRC msgids and XMPP stanza-ids are only unique within their generating
    entity, so bare strings collide across networks and rooms. Equality and
    hashing ignore ``by``: XEP-0461 replies and XEP-0444 reactions usually
    carry only the id, while XEP-0359 uniqueness is ``(id, by)``. Lookups
    without ``by`` still match the stored ref for that connection/room/id.
    """
    conn: str
    room: str
    id: str
    by: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "room", self.room.lower())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MsgRef):
            return NotImplemented
        return (self.conn, self.room, self.id) == (other.conn, other.room, other.id)

    def __hash__(self) -> int:
        return hash((self.conn, self.room, self.id))


def irc_ref(conn: str, channel: str, msgid: str) -> MsgRef:
    return MsgRef(conn=conn, room=channel, id=msgid)


def xmpp_ref(
    conn: str, muc_jid: str, ident: str, by: str | None = None
) -> MsgRef:
    return MsgRef(conn=conn, room=muc_jid, id=ident, by=by)


_K = TypeVar("_K")
_V = TypeVar("_V")


def _lru_put(mapping: collections.OrderedDict[_K, _V], key: _K, value: _V) -> None:
    mapping[key] = value
    if len(mapping) > _NICK_MAP_SIZE:
        mapping.popitem(last=False)


def _lru_touch(mapping: collections.OrderedDict[_K, _V], key: _K) -> None:
    if key in mapping:
        mapping.move_to_end(key)
    elif len(mapping) >= _NICK_MAP_SIZE:
        mapping.popitem(last=False)


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

        # Maps XMPP message ref -> original IRC nick, so replies to bridged
        # messages can resolve the actual IRC user instead of the bot nick.
        self._msg_id_to_irc_nick: collections.OrderedDict[MsgRef, str] = (
            collections.OrderedDict()
        )

        # Reply threading (IRC msgid ref → XMPP stanza-id ref)
        # Temp: XMPP client_id ref → IRC msgid ref (cleared when stanza-id arrives)
        self._xmpp_cid_to_irc_msgid: dict[MsgRef, MsgRef] = {}
        # Final: IRC msgid ref → XMPP stanza-id ref (used when IRC user replies)
        self._irc_msgid_to_xmpp_sid: collections.OrderedDict[MsgRef, MsgRef] = (
            collections.OrderedDict()
        )
        # Reverse: XMPP stanza-id ref → IRC msgid ref (for XMPP→IRC native reply tags)
        self._xmpp_sid_to_irc_msgid: collections.OrderedDict[MsgRef, MsgRef] = (
            collections.OrderedDict()
        )
        # Pending XMPP stanza-id refs awaiting IRC echo-message, per sending
        # connection (id(client), channel). Master and each puppet are
        # separate sockets, so they must not share a FIFO.
        self._pending_echo_sids: dict[
            tuple[int, str], collections.deque[MsgRef]
        ] = {}

        # Sender MUC occupant JID tracking for XEP-0461 reply "to" attribute
        # Temp: XMPP client_id ref → sender MUC occupant JID
        self._xmpp_cid_to_sender_jid: dict[MsgRef, str] = {}
        # Final: XMPP stanza-id ref → sender MUC occupant JID
        self._xmpp_sid_to_sender_jid: collections.OrderedDict[MsgRef, str] = (
            collections.OrderedDict()
        )

        # Reaction state for IRC → XMPP: irc msgid ref → {nick: current emoji set}
        self._irc_reaction_state: collections.OrderedDict[
            MsgRef, dict[str, set[str]]
        ] = collections.OrderedDict()

        # Reaction state for XMPP → IRC: xmpp sid ref → {nick: previous emoji frozenset}
        self._xmpp_reaction_state: collections.OrderedDict[
            MsgRef, dict[str, frozenset[str]]
        ] = collections.OrderedDict()

        # Body cache for inline reply excerpts: XMPP stanza-id ref → message body.
        # Populated for both XMPP messages (on receipt) and IRC messages (once
        # the echo maps client_id → stanza-id in the XMPP self-message handler).
        self._body_cache: collections.OrderedDict[MsgRef, str] = (
            collections.OrderedDict()
        )
        # Temp: XMPP client_id ref → body (until stanza-id is known)
        self._pending_body: dict[MsgRef, str] = {}
        # Incoming XMPP client-id / origin-id → stanza-id (for XEP-0308 replace)
        self._xmpp_alt_id_to_sid: collections.OrderedDict[MsgRef, MsgRef] = (
            collections.OrderedDict()
        )

        self._stopping: bool = False
        self.puppet_pool: IrcPuppetPool | None = None
        # Real XMPP occupants per (xmpp_name, muc_jid.lower())
        self._xmpp_occupants: dict[tuple[str, str], set[str]] = {}

    def _setting(self, b: BridgeMapping, name: str):
        """Resolve a setting: per-bridge override if set, else global."""
        val = getattr(b, name, None)
        if val is not None:
            return val
        return getattr(self.config.settings, name)

    def _sync_bans(self, b: BridgeMapping) -> bool:
        """Whether to sync XMPP puppet bans to IRC for this mapping.

        Most specific wins: bridge > irc network > xmpp account > global.
        """
        if b.sync_bans is not None:
            return b.sync_bans
        irc_cfg = self.config.irc_by_name(b.irc)
        if irc_cfg.sync_bans is not None:
            return irc_cfg.sync_bans
        xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
        if xmpp_cfg.sync_bans is not None:
            return xmpp_cfg.sync_bans
        return self.config.settings.sync_bans

    def _puppet_mode(self, irc_cfg: IRCConfig) -> str:
        return resolve_puppet_mode(irc_cfg, self.config.settings)

    def _owned_irc_nick(self, irc_name: str, nick: str) -> bool:
        return (
            self.puppet_pool is not None
            and self.puppet_pool.is_owned(irc_name, nick)
        )

    def _wants_relaymsg(
        self, irc_cfg: IRCConfig, irc_client: IRCClient, channel: str
    ) -> bool:
        if self._puppet_mode(irc_cfg) in ("nicks", "prefix"):
            return False
        return bool(irc_cfg.relaymsg and irc_client.can_relaymsg(channel))

    def _wants_puppet(
        self, irc_cfg: IRCConfig, irc_client: IRCClient, channel: str
    ) -> bool:
        if self.puppet_pool is None:
            return False
        mode = self._puppet_mode(irc_cfg)
        if mode == "nicks":
            return True
        if mode == "auto":
            return not self._wants_relaymsg(irc_cfg, irc_client, channel)
        return False

    def _puppet_presence(self, irc_cfg: IRCConfig) -> str:
        return resolve_puppet_presence(irc_cfg, self.config.settings)

    async def _maybe_puppet(
        self,
        irc_cfg: IRCConfig,
        irc_client: IRCClient,
        channel: str,
        nick: str,
        *,
        ignore_kick: bool = True,
    ) -> IRCClient | None:
        if not self._wants_puppet(irc_cfg, irc_client, channel):
            return None
        assert self.puppet_pool is not None
        return await self.puppet_pool.acquire(
            irc_cfg.name, nick, channel, ignore_kick=ignore_kick,
        )

    def _cache_body(self, ref: MsgRef, body: str) -> None:
        """Store a message body in the bounded reply-excerpt cache."""
        _lru_put(self._body_cache, ref, body)

    def _xmpp_sid_ref(
        self, xmpp_name: str, muc_jid: str, ident: str, by: str | None = None
    ) -> MsgRef:
        """Build a stanza-id ref, defaulting ``by`` to the MUC JID when omitted."""
        return xmpp_ref(xmpp_name, muc_jid, ident, by=by or muc_jid)

    def stop(self) -> None:
        self._stopping = True
        for client in self.xmpp_clients.values():
            client.stop()

    async def shutdown(self) -> None:
        if self.puppet_pool is not None:
            await self.puppet_pool.shutdown()

    async def start(self) -> None:
        self._build_clients()
        self.puppet_pool = IrcPuppetPool(
            self.config,
            self.irc_clients,
            on_self_message=self._make_irc_self_msg_handler,
            on_dropped=self._on_puppet_dropped,
        )
        self._build_lookup_tables()
        self._wire_callbacks()
        await self._connect_all()

    def _build_clients(self) -> None:
        for xmpp_cfg in self.config.xmpp:
            avatar = self._load_avatar(xmpp_cfg)
            hide_version = self.config.settings.hide_version
            hide_os = self.config.settings.hide_os
            if xmpp_cfg.component:
                client: XMPPClient | XMPPComponent = XMPPComponent(
                    component_domain=xmpp_cfg.component_domain,  # type: ignore[arg-type]
                    password=xmpp_cfg.password,
                    component_host=xmpp_cfg.component_host,
                    component_port=xmpp_cfg.component_port,
                    nick=xmpp_cfg.nick,
                    avatar=avatar,
                    hide_version=hide_version,
                    hide_os=hide_os,
                )
                self.xmpp_components[xmpp_cfg.name] = client  # type: ignore[assignment]
            else:
                client = XMPPClient(
                    jid=xmpp_cfg.jid,
                    password=xmpp_cfg.password,
                    nick=xmpp_cfg.nick,
                    avatar=avatar,
                    hide_version=hide_version,
                    hide_os=hide_os,
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
                relaymsg_suffix=(
                    irc_cfg.relaymsg_suffix
                    if irc_cfg.relaymsg_suffix is not None
                    else self.config.settings.relaymsg_suffix
                ),
            )
            self.irc_clients[irc_cfg.name] = client

    def _load_avatar(self, xmpp_cfg) -> Avatar | None:
        """Resolve and load the bot avatar for an XMPP config.

        None in config -> bundled default; "" -> explicitly disabled; otherwise
        a path or URL.  A load failure logs and returns None so a bad avatar
        never blocks the bridge from starting.
        """
        source = xmpp_cfg.avatar
        if source == "":
            return None
        if source is None:
            source = default_avatar_path()
            if source is None:
                return None
        try:
            return Avatar.load(
                source,
                byte_cap=self.config.settings.avatar_byte_cap,
                target_px=self.config.settings.avatar_target_px,
            )
        except Exception as e:
            log.warning(
                "Avatar for XMPP %s not loaded (%s); continuing without",
                xmpp_cfg.name, e,
            )
            return None

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
            client.on_self_message = self._make_irc_self_msg_handler(irc_name, client)
            client.on_reaction = self._make_irc_reaction_handler(irc_name)
            # Presence callbacks for component-backed bridges
            client.on_user_join = self._make_irc_join_handler(irc_name)
            client.on_user_part = self._make_irc_part_handler(irc_name)
            client.on_user_quit = self._make_irc_quit_handler(irc_name)
            client.on_user_nick = self._make_irc_nick_handler(irc_name)
            client.on_names_done = self._make_irc_names_handler(irc_name)

        for xmpp_name, client in self.xmpp_clients.items():
            client.on_message = self._make_xmpp_handler(xmpp_name)
            client.on_self_message = self._make_xmpp_self_msg_handler(xmpp_name)
            client.on_reaction = self._make_xmpp_reaction_handler(xmpp_name)
            client.on_typing = self._make_xmpp_typing_handler(xmpp_name)
            client.on_occupant = self._make_xmpp_occupant_handler(xmpp_name)

        for xmpp_name, component in self.xmpp_components.items():
            component.on_reconnected = self._make_xmpp_reconnect_handler(xmpp_name)
            component.on_puppet_banned = self._make_puppet_banned_handler(xmpp_name)
            component.on_occupant = self._make_xmpp_occupant_handler(xmpp_name)

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
        if self._stopping:
            return
        delay = _RECONNECT_BASE
        while True:
            log.info(
                "Reconnecting to IRC %s in %d seconds...", name, delay
            )
            await asyncio.sleep(delay)
            if self._stopping:
                return
            try:
                await self._do_connect_irc(name, client)
                log.info("Reconnected to IRC %s", name)
                return
            except (OSError, ConnectionRefusedError) as e:
                log.warning("IRC reconnect failed for %s: %s", name, e)
                delay = min(delay * 2, _RECONNECT_MAX)

    # ---------- IRC -> XMPP ----------

    def _record_msg_id(self, ref: MsgRef, irc_nick: str) -> None:
        log.debug("Recording %s -> irc_nick=%s", ref, irc_nick)
        _lru_put(self._msg_id_to_irc_nick, ref, irc_nick)

    def _make_xmpp_self_msg_handler(self, xmpp_name: str):
        async def handler(
            muc_jid: str,
            client_id: str,
            stanza_id: str,
            stanza_id_by: str | None,
        ) -> None:
            """Re-key nick and IRC msgid mappings when we learn the server stanza-id."""
            cid = xmpp_ref(xmpp_name, muc_jid, client_id)
            sid = self._xmpp_sid_ref(xmpp_name, muc_jid, stanza_id, stanza_id_by)

            irc_nick = self._msg_id_to_irc_nick.pop(cid, None)
            if irc_nick:
                log.debug(
                    "Re-keying %s -> %s for nick=%s",
                    cid, sid, irc_nick,
                )
                self._msg_id_to_irc_nick[sid] = irc_nick

            sender_jid = self._xmpp_cid_to_sender_jid.pop(cid, None)
            if sender_jid:
                _lru_put(self._xmpp_sid_to_sender_jid, sid, sender_jid)

            irc_msgid = self._xmpp_cid_to_irc_msgid.pop(cid, None)
            if irc_msgid:
                _lru_put(self._irc_msgid_to_xmpp_sid, irc_msgid, sid)
                _lru_put(self._xmpp_sid_to_irc_msgid, sid, irc_msgid)

            body = self._pending_body.pop(cid, None)
            if body is not None:
                self._cache_body(sid, body)

        return handler

    def _strip_reply_prefix(self, text: str, xmpp_sid: MsgRef) -> str:
        """Strip the IRC client's 'nick: ' reply fallback when sending a native reply."""
        return _strip_nick_prefix(text, self._msg_id_to_irc_nick.get(xmpp_sid))

    def _make_irc_message_handler(self, irc_name: str):
        async def handler(irc_msg: IRCMessage) -> None:
            await self._forward_irc_to_xmpp(irc_name, irc_msg)

        return handler

    def _make_irc_action_handler(self, irc_name: str):
        async def handler(irc_msg: IRCMessage) -> None:
            await self._forward_irc_to_xmpp(irc_name, irc_msg)

        return handler

    async def _forward_irc_to_xmpp(self, irc_name: str, irc_msg: IRCMessage) -> None:
        channel, nick, text = irc_msg.channel, irc_msg.nick, irc_msg.text
        if self._owned_irc_nick(irc_name, nick):
            return
        text = format_irc_to_xmpp(text)
        key = (irc_name, channel.lower())
        bridges = self._irc_to_bridges.get(key, [])
        for b in bridges:
            xmpp_sid = (
                self._irc_msgid_to_xmpp_sid.get(
                    irc_ref(irc_name, channel, irc_msg.reply_to_msgid)
                )
                if irc_msg.reply_to_msgid
                else None
            )
            reply_to_sender = (
                self._xmpp_sid_to_sender_jid.get(xmpp_sid)
                if xmpp_sid
                else None
            )
            relay_text = self._strip_reply_prefix(text, xmpp_sid) if xmpp_sid else text
            if irc_msg.is_action:
                puppet_body = f"/me {relay_text}"
            else:
                puppet_body = relay_text

            if b.xmpp in self.xmpp_components:
                component = self.xmpp_components[b.xmpp]
                xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
                irc_cfg = self.config.irc_by_name(irc_name)
                pjid = _puppet_jid(nick, irc_cfg.name, xmpp_cfg.component_domain)  # type: ignore[arg-type]
                if xmpp_sid:
                    msg_id = await component.send_puppet_reply(
                        b.xmpp_muc, pjid, puppet_body, xmpp_sid.id,
                        reply_to=reply_to_sender,
                    )
                else:
                    msg_id = await component.send_puppet_message(
                        b.xmpp_muc, pjid, puppet_body,
                    )
                if not msg_id:
                    continue
                cid = xmpp_ref(b.xmpp, b.xmpp_muc, msg_id)
                self._record_msg_id(cid, nick)
                self._pending_body[cid] = relay_text
                if irc_msg.msgid:
                    self._xmpp_cid_to_irc_msgid[cid] = irc_ref(
                        irc_name, channel, irc_msg.msgid
                    )
                actual_nick = component._puppet_nicks.get(
                    b.xmpp_muc.lower(), {}
                ).get(pjid, nick)
                self._xmpp_cid_to_sender_jid[cid] = f"{b.xmpp_muc}/{actual_nick}"
            else:
                xmpp_client = self.xmpp_clients[b.xmpp]
                display_nick = (
                    anti_ping(nick) if self._setting(b, "anti_ping") else nick
                )
                if irc_msg.is_action:
                    plumbing_body = f"* {display_nick} {relay_text}"
                else:
                    plumbing_body = f"<{display_nick}> {relay_text}"
                await xmpp_client.send_typing(b.xmpp_muc, False)
                if xmpp_sid:
                    msg_id = await xmpp_client.send_reply(
                        b.xmpp_muc, plumbing_body, xmpp_sid.id,
                        reply_to=reply_to_sender,
                    )
                else:
                    msg_id = await xmpp_client.send_message(
                        b.xmpp_muc, plumbing_body
                    )
                cid = xmpp_ref(b.xmpp, b.xmpp_muc, msg_id)
                self._record_msg_id(cid, nick)
                self._pending_body[cid] = relay_text
                if irc_msg.msgid:
                    self._xmpp_cid_to_irc_msgid[cid] = irc_ref(
                        irc_name, channel, irc_msg.msgid
                    )
                self._xmpp_cid_to_sender_jid[cid] = f"{b.xmpp_muc}/{xmpp_client.nick}"

    def _make_irc_typing_handler(self, irc_name: str):
        async def handler(channel: str, nick: str, is_typing: bool) -> None:
            if self._owned_irc_nick(irc_name, nick):
                return
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
            if self._owned_irc_nick(irc_name, nick):
                return
            key = (irc_name, channel.lower())
            for b in self._irc_to_bridges.get(key, []):
                args = self._puppet_args(b, irc_name, nick)
                if args:
                    component, muc_jid, pjid = args
                    await component.join_puppet(muc_jid, pjid, nick)

        return handler

    def _make_irc_part_handler(self, irc_name: str):
        async def handler(channel: str, nick: str) -> None:
            if self._owned_irc_nick(irc_name, nick):
                return
            key = (irc_name, channel.lower())
            for b in self._irc_to_bridges.get(key, []):
                args = self._puppet_args(b, irc_name, nick)
                if args:
                    component, muc_jid, pjid = args
                    await component.part_puppet(muc_jid, pjid, nick)

        return handler

    def _make_irc_quit_handler(self, irc_name: str):
        async def handler(nick: str, channels: list[str]) -> None:
            if self._owned_irc_nick(irc_name, nick):
                return
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
            if self._owned_irc_nick(irc_name, old_nick) or self._owned_irc_nick(
                irc_name, new_nick
            ):
                return
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
            for occ_key in [k for k in self._xmpp_occupants if k[0] == xmpp_name]:
                self._xmpp_occupants.pop(occ_key, None)
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
                    if self._owned_irc_nick(irc_name, nick):
                        continue
                    pjid = _puppet_jid(nick, irc_cfg.name, domain)
                    asyncio.create_task(
                        component.join_puppet(b.xmpp_muc, pjid, nick)
                    )

        return handler

    def _make_puppet_banned_handler(self, xmpp_name: str):
        """When a component puppet is banned in a MUC, +b and kick on IRC."""
        async def handler(
            muc_jid: str,
            puppet_jid: str,
            irc_nick: str,
            reason: str | None,
            actor: str | None,
        ) -> None:
            key = (xmpp_name, muc_jid.lower())
            kick_reason = _ban_kick_reason(reason, actor)
            for b in self._xmpp_to_bridges.get(key, []):
                if not self._sync_bans(b):
                    log.debug(
                        "sync_bans off for %s ↔ %s, not banning %s",
                        b.xmpp_muc, b.irc_channel, irc_nick,
                    )
                    continue
                irc_client = self.irc_clients.get(b.irc)
                if irc_client is None:
                    continue
                log.info(
                    "Syncing XMPP ban of %s (%s) to %s on %s",
                    puppet_jid, irc_nick, b.irc_channel, b.irc,
                )
                try:
                    await irc_client.ban_and_kick(
                        b.irc_channel, irc_nick, kick_reason
                    )
                except Exception as e:
                    log.warning(
                        "Failed to sync ban of %s to %s on %s: %s",
                        irc_nick, b.irc_channel, b.irc, e,
                    )

        return handler

    def _make_irc_away_handler(self, irc_name: str):
        async def handler(nick: str, channels: list[str], reason: str | None) -> None:
            if self._owned_irc_nick(irc_name, nick):
                return
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
            if xmpp_name in self.xmpp_components:
                if self.xmpp_components[xmpp_name].is_puppet_nick(muc_jid, nick):
                    return
            key = (xmpp_name, muc_jid.lower())
            bridges = self._xmpp_to_bridges.get(key, [])
            for b in bridges:
                irc_client = self.irc_clients[b.irc]
                irc_cfg = self.config.irc_by_name(b.irc)
                puppet = (
                    self.puppet_pool.get_connected(b.irc, nick)
                    if self.puppet_pool is not None
                    else None
                )
                if puppet is not None:
                    assert self.puppet_pool is not None
                    self.puppet_pool.touch(b.irc, nick)
                    await puppet.send_typing(b.irc_channel, is_typing)
                elif self._wants_puppet(irc_cfg, irc_client, b.irc_channel):
                    # Don't spawn a socket just to type, and don't type as the bot.
                    continue
                else:
                    await irc_client.send_typing(b.irc_channel, is_typing)

        return handler

    def _make_xmpp_occupant_handler(self, xmpp_name: str):
        async def handler(event: OccupantEvent) -> None:
            if event.is_self:
                return
            if xmpp_name in self.xmpp_components:
                component = self.xmpp_components[xmpp_name]
                if component.is_puppet_nick(event.muc_jid, event.nick):
                    return
                if event.new_nick and component.is_puppet_nick(
                    event.muc_jid, event.new_nick
                ):
                    return

            occ_key = (xmpp_name, event.muc_jid.lower())
            occupants = self._xmpp_occupants.setdefault(occ_key, set())
            key = (xmpp_name, event.muc_jid.lower())
            bridges = self._xmpp_to_bridges.get(key, [])

            if event.kind == "leave":
                occupants.discard(event.nick)
                quit_reason = _occupant_irc_quit_reason(event)
                for b in bridges:
                    await self._release_occupant_puppet(
                        b, event.nick, quit_reason=quit_reason,
                    )
                return

            if event.kind == "nick" and event.new_nick:
                occupants.discard(event.nick)
                occupants.add(event.new_nick)
                if self.puppet_pool is not None:
                    for b in bridges:
                        irc_client = self.irc_clients.get(b.irc)
                        if irc_client is None:
                            continue
                        irc_cfg = self.config.irc_by_name(b.irc)
                        if not self._wants_puppet(irc_cfg, irc_client, b.irc_channel):
                            continue
                        await self.puppet_pool.rename(
                            b.irc, event.nick, event.new_nick,
                        )
                return

            # join (and subsequent available / away updates)
            occupants.add(event.nick)
            for b in bridges:
                irc_client = self.irc_clients.get(b.irc)
                if irc_client is None:
                    continue
                irc_cfg = self.config.irc_by_name(b.irc)
                if not self._wants_puppet(irc_cfg, irc_client, b.irc_channel):
                    continue
                assert self.puppet_pool is not None
                if self._puppet_presence(irc_cfg) == "eager":
                    await self.puppet_pool.acquire(
                        b.irc, event.nick, b.irc_channel, ignore_kick=False,
                    )
                await self.puppet_pool.set_away(
                    b.irc, event.nick, event.away_reason,
                )

        return handler

    async def _release_occupant_puppet(
        self, b: BridgeMapping, xmpp_nick: str,
        *,
        quit_reason: str | None = None,
    ) -> None:
        if self.puppet_pool is None:
            return
        irc_client = self.irc_clients.get(b.irc)
        if irc_client is None:
            return
        irc_cfg = self.config.irc_by_name(b.irc)
        if not self._wants_puppet(irc_cfg, irc_client, b.irc_channel):
            return
        await self.puppet_pool.release(
            b.irc, xmpp_nick, b.irc_channel, quit_reason=quit_reason,
        )

    async def _on_puppet_dropped(
        self, irc_name: str, xmpp_nick: str, channels: list[str]
    ) -> None:
        if self._stopping or self.puppet_pool is None:
            return
        irc_cfg = self.config.irc_by_name(irc_name)
        if self._puppet_presence(irc_cfg) != "eager":
            return
        irc_client = self.irc_clients.get(irc_name)
        if irc_client is None:
            return
        for channel in channels:
            if self.puppet_pool.was_kicked(irc_name, xmpp_nick, channel):
                continue
            for b in self._irc_to_bridges.get((irc_name, channel.lower()), []):
                if not self._wants_puppet(irc_cfg, irc_client, b.irc_channel):
                    continue
                occ = self._xmpp_occupants.get(
                    (b.xmpp, b.xmpp_muc.lower()), set()
                )
                if xmpp_nick not in occ:
                    continue
                await self.puppet_pool.acquire(
                    irc_name, xmpp_nick, b.irc_channel, ignore_kick=False,
                )

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

            sid = (
                self._xmpp_sid_ref(
                    xmpp_name, msg.muc_jid, msg.stanza_id, msg.stanza_id_by
                )
                if msg.stanza_id
                else None
            )
            if sid:
                self._cache_body(sid, msg.body)
                self._index_xmpp_alt_ids(xmpp_name, msg, sid)

            for b in bridges:
                irc_client = self.irc_clients[b.irc]
                irc_cfg = self.config.irc_by_name(b.irc)
                if msg.is_action:
                    sender = await self._relay_action_to_irc(
                        irc_client, irc_cfg, b.irc_channel, msg, b, xmpp_name
                    )
                else:
                    sender = await self._relay_to_irc(
                        irc_client, irc_cfg, b.irc_channel, msg, b, xmpp_name
                    )
                if sid:
                    _lru_put(
                        self._xmpp_sid_to_sender_jid,
                        sid,
                        f"{msg.muc_jid}/{msg.nick}",
                    )
                    # Queue keyed by sending connection so a puppet echo cannot
                    # steal the master's pending sid (or vice versa).
                    if sender.has_echo_message:
                        q = self._pending_echo_sids.setdefault(
                            self._echo_key(sender, b.irc_channel),
                            collections.deque(maxlen=_NICK_MAP_SIZE),
                        )
                        q.append(sid)

        return handler

    def _index_xmpp_alt_ids(
        self, xmpp_name: str, msg: XMPPMessage, sid: MsgRef
    ) -> None:
        """Map client-id / origin-id onto the stanza-id for later replace lookups."""
        for raw in (msg.client_id, msg.origin_id):
            if raw and raw != sid.id:
                _lru_put(
                    self._xmpp_alt_id_to_sid,
                    xmpp_ref(xmpp_name, msg.muc_jid, raw),
                    sid,
                )

    def _irc_msgid_for_xmpp_id(
        self, xmpp_name: str | None, muc_jid: str, ident: str | None
    ) -> str | None:
        """Resolve an XMPP id (stanza-id, client id, or origin-id) to an IRC msgid."""
        if not xmpp_name or not ident:
            return None
        ref = xmpp_ref(xmpp_name, muc_jid, ident)
        mapped = self._xmpp_sid_to_irc_msgid.get(ref)
        if mapped:
            return mapped.id
        sid = self._xmpp_alt_id_to_sid.get(ref)
        if sid is not None:
            mapped = self._xmpp_sid_to_irc_msgid.get(sid)
            if mapped:
                return mapped.id
        return None

    def _irc_reply_to_for_xmpp(self, xmpp_name: str | None, msg: XMPPMessage) -> str | None:
        """IRC msgid for +reply: XEP-0308 replace target, else XEP-0461 reply."""
        if msg.replace_id:
            return self._irc_msgid_for_xmpp_id(
                xmpp_name, msg.muc_jid, msg.replace_id
            )
        return self._irc_msgid_for_xmpp_id(
            xmpp_name, msg.muc_jid, msg.reply_to_id
        )

    @staticmethod
    def _echo_key(client: IRCClient, channel: str) -> tuple[int, str]:
        return (id(client), channel.lower())

    def _make_irc_self_msg_handler(self, irc_name: str, client: IRCClient):
        """Handle echo-message: map IRC msgid → XMPP stanza-id for reply threading.

        Bound to a single IRC connection (master or one puppet). Echoes on
        that socket pop from that connection's queue only.
        """
        async def handler(channel: str, msgid: str) -> None:
            q = self._pending_echo_sids.get(self._echo_key(client, channel))
            if q:
                xmpp_sid = q.popleft()
                irc_msgid = irc_ref(irc_name, channel, msgid)
                _lru_put(self._irc_msgid_to_xmpp_sid, irc_msgid, xmpp_sid)
                _lru_put(self._xmpp_sid_to_irc_msgid, xmpp_sid, irc_msgid)

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
        b: BridgeMapping,
        xmpp_name: str | None = None,
    ) -> str:
        """Build a reply prefix for smart replies.

        With reply_style='quote' (default) and a cache hit, returns:
            "nick: [excerpt] "
        Falls back to ping-only "nick: " when the cache misses or
        reply_style='ping'.
        """
        if not msg.reply_to_nick:
            return ""

        reply_ref = (
            xmpp_ref(xmpp_name, msg.muc_jid, msg.reply_to_id)
            if xmpp_name and msg.reply_to_id
            else None
        )

        log.debug(
            "Reply lookup: reply_to_id=%s, known_ids=%s",
            reply_ref,
            list(self._msg_id_to_irc_nick.keys())[-5:],
        )

        # Resolve the target IRC nick
        if reply_ref and reply_ref in self._msg_id_to_irc_nick:
            # Replied-to message was bridged from IRC; use the real IRC nick
            target = self._msg_id_to_irc_nick[reply_ref]
        else:
            target = msg.reply_to_nick
            # Puppet nicks are already real IRC users; only native XMPP users
            # (or MUCs with no component) get the relaymsg suffix.
            if xmpp_name in self.xmpp_components:
                component = self.xmpp_components[xmpp_name]
                is_native = not component.is_puppet_nick(msg.muc_jid, target)
            else:
                is_native = True
            if is_native:
                mode = self._puppet_mode(irc_cfg)
                use_nicks = mode == "nicks" or (
                    mode == "auto"
                    and not (irc_cfg.relaymsg and irc_client.has_relaymsg)
                )
                if use_nicks and self.puppet_pool is not None:
                    target = self.puppet_pool.display_nick(irc_cfg.name, target)
                elif irc_cfg.relaymsg and irc_client.has_relaymsg:
                    target = _relaymsg_nick(
                        target,
                        irc_client.relaymsg_separator,
                        irc_client.relaymsg_suffix,
                    )

        style = self._setting(b, "reply_style")
        cached_body = self._body_cache.get(reply_ref) if reply_ref else None
        return _build_reply_prefix(target, style, cached_body)

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
    ) -> IRCClient:
        text = msg.body
        if msg.is_correction:
            text = self._format_correction(text)

        irc_reply_to = self._irc_reply_to_for_xmpp(xmpp_name, msg)

        puppet = await self._maybe_puppet(irc_cfg, irc_client, channel, msg.nick)
        sender = puppet if puppet is not None else irc_client
        as_puppet = puppet is not None

        # Corrections are marked with *; skip the quote/ping crutch.
        # Connected puppets with +reply do not need it either.
        if msg.is_correction or (as_puppet and irc_reply_to):
            reply_prefix = ""
        else:
            reply_prefix = self._format_reply_prefix(
                msg, irc_client, irc_cfg, b, xmpp_name
            )

        # Split into logical lines, then break any line that would overflow
        # the IRC line-length limit into byte-sized pieces. The pieces count
        # as ordinary lines below, so an oversized paste still hits max_lines
        # (and thus the pastebin fallback) rather than flooding the channel.
        body_budget = self._irc_body_budget(
            sender, irc_cfg, channel, msg, b, reply_prefix, for_puppet=as_puppet,
        )
        lines: list[str] = []
        for logical_line in text.split("\n"):
            lines.extend(_split_to_byte_limit(logical_line, body_budget))

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
                    reply_to=irc_reply_to, sender=puppet,
                )
                return sender
            # Fall through to truncated relay if upload fails

        if max_lines > 0:
            lines = lines[:max_lines]

        # Drop blank lines — IRC PRIVMSG bodies cannot be empty, and the
        # prefixed format already conveys structure line-by-line.
        non_blank = [line for line in lines if line.strip()]

        use_relaymsg = self._wants_relaymsg(irc_cfg, irc_client, channel) and not as_puppet
        if as_puppet and len(non_blank) > 1:
            first = reply_prefix + non_blank[0]
            batch_lines = [first] + non_blank[1:]
            if sender.can_multiline(batch_lines):
                await sender.send_multiline_message(
                    channel, batch_lines, reply_to=irc_reply_to,
                )
                return sender
        if not use_relaymsg and not as_puppet and len(non_blank) > 1:
            display_nick = (
                anti_ping(msg.nick)
                if self._setting(b, "anti_ping")
                else msg.nick
            )
            first_prefix = f"<{display_nick}> {reply_prefix}"
            batch_lines = [first_prefix + non_blank[0]] + non_blank[1:]
            if irc_client.can_multiline(batch_lines):
                await irc_client.send_multiline_message(
                    channel, batch_lines, reply_to=irc_reply_to,
                )
                return irc_client

        for i, line in enumerate(non_blank):
            # Only prepend reply prefix and native reply tag to the first line
            prefix = reply_prefix if i == 0 else ""
            reply_tag = irc_reply_to if i == 0 else None
            await self._send_irc_line(
                irc_client, irc_cfg, channel, msg.nick, f"{prefix}{line}", b,
                reply_to=reply_tag, sender=puppet,
            )
        return sender

    def _irc_body_budget(
        self,
        irc_client: IRCClient,
        irc_cfg: IRCConfig,
        channel: str,
        msg: XMPPMessage,
        b: BridgeMapping,
        reply_prefix: str,
        *,
        for_puppet: bool = False,
    ) -> int:
        """Max UTF-8 bytes of message text that fit in one IRC line to `channel`.

        Starts from the line-length ceiling and subtracts everything the wire
        line carries besides the text itself: CRLF, the ``PRIVMSG <chan> :``
        framing, the ``<nick> `` display prefix we prepend (skipped for
        connected puppets), the reply excerpt, and a reserve for the source
        prefix the server adds when relaying onward.

        The ceiling is a ``max_line_bytes`` override resolved most-specific
        first — per-bridge, then per-IRC-network, then global — and finally the
        auto-detected server limit (ISUPPORT LINELEN, default 512) when unset.
        """
        line_len = irc_client.line_len
        for override in (
            b.max_line_bytes,
            irc_cfg.max_line_bytes,
            self.config.settings.max_line_bytes,
        ):
            if override and override > 0:
                line_len = override
                break

        nick_prefix = 0
        if not for_puppet:
            display_nick = (
                anti_ping(msg.nick) if self._setting(b, "anti_ping") else msg.nick
            )
            nick_prefix = len(f"<{display_nick}> ".encode("utf-8"))
        overhead = (
            2  # trailing CRLF
            + _SOURCE_PREFIX_RESERVE
            + len(b"PRIVMSG ")
            + len(channel.encode("utf-8"))
            + len(b" :")
            + nick_prefix
            + len(reply_prefix.encode("utf-8"))
        )
        return max(_MIN_LINE_BUDGET, line_len - overhead)

    async def _send_irc_line(
        self,
        irc_client: IRCClient,
        irc_cfg: IRCConfig,
        channel: str,
        nick: str,
        text: str,
        b: BridgeMapping,
        reply_to: str | None = None,
        sender: IRCClient | None = None,
    ) -> None:
        if sender is not None and sender is not irc_client:
            await sender.send_message(channel, text, reply_to=reply_to)
            return
        if self._wants_relaymsg(irc_cfg, irc_client, channel):
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

    # ---------- Reaction bridging ----------

    def _make_irc_reaction_handler(self, irc_name: str):
        async def handler(
            channel: str,
            nick: str,
            emoji: str,
            reply_to_msgid: str | None,
            is_unreact: bool,
        ) -> None:
            if not reply_to_msgid:
                return
            if self._owned_irc_nick(irc_name, nick):
                return
            key = (irc_name, channel.lower())
            bridges = self._irc_to_bridges.get(key, [])
            if not bridges:
                return

            target = irc_ref(irc_name, channel, reply_to_msgid)
            # Update LRU state: irc msgid ref → nick → current emoji set
            _lru_touch(self._irc_reaction_state, target)
            nick_map = self._irc_reaction_state.setdefault(target, {})
            current = nick_map.get(nick, set())
            if is_unreact:
                current.discard(emoji)
            else:
                current.add(emoji)
            nick_map[nick] = current

            xmpp_sid = self._irc_msgid_to_xmpp_sid.get(target)
            if not xmpp_sid:
                log.debug(
                    "No XMPP stanza-id for IRC reaction to msgid=%s, dropping",
                    reply_to_msgid,
                )
                return

            for b in bridges:
                if b.xmpp in self.xmpp_components:
                    component = self.xmpp_components[b.xmpp]
                    xmpp_cfg = self.config.xmpp_by_name(b.xmpp)
                    irc_cfg = self.config.irc_by_name(irc_name)
                    pjid = _puppet_jid(nick, irc_cfg.name, xmpp_cfg.component_domain)  # type: ignore[arg-type]
                    await component.send_puppet_reaction(
                        b.xmpp_muc, pjid, xmpp_sid.id, frozenset(current)
                    )
                else:
                    xmpp_client = self.xmpp_clients[b.xmpp]
                    await xmpp_client.send_reaction(
                        b.xmpp_muc, xmpp_sid.id, frozenset(current)
                    )

        return handler

    def _format_reaction_text(
        self, b: BridgeMapping, stanza_ref: MsgRef, emoji: str
    ) -> str:
        """Build an IRC text body for a bridged XMPP reaction.

        With reply_style='quote' and a cache hit, returns:
            (reacted to "excerpt") <emoji>
        Falls back to:
            reacted <emoji>
        """
        style = self._setting(b, "reply_style")
        return _build_reaction_text(
            emoji, style, self._body_cache.get(stanza_ref)
        )

    def _make_xmpp_reaction_handler(self, xmpp_name: str):
        async def handler(
            muc_jid: str,
            nick: str,
            stanza_id_ref: str,
            emojis: frozenset[str],
        ) -> None:
            # In component mode, filter out puppet reactions
            if xmpp_name in self.xmpp_components:
                component = self.xmpp_components[xmpp_name]
                if component.is_puppet_nick(muc_jid, nick):
                    return

            key = (xmpp_name, muc_jid.lower())
            bridges = self._xmpp_to_bridges.get(key, [])
            if not bridges:
                return

            sid_ref = xmpp_ref(xmpp_name, muc_jid, stanza_id_ref)
            # Compute delta vs previously bridged state for this sender
            _lru_touch(self._xmpp_reaction_state, sid_ref)
            state = self._xmpp_reaction_state.setdefault(sid_ref, {})
            prev = state.get(nick, frozenset())
            added = emojis - prev
            removed = prev - emojis
            state[nick] = emojis

            irc_msgid_ref = self._xmpp_sid_to_irc_msgid.get(sid_ref)
            irc_msgid = irc_msgid_ref.id if irc_msgid_ref else None

            for b in bridges:
                irc_client = self.irc_clients[b.irc]
                irc_cfg = self.config.irc_by_name(b.irc)
                puppet = await self._maybe_puppet(
                    irc_cfg, irc_client, b.irc_channel, nick,
                )
                if puppet is not None and irc_msgid:
                    native = True
                    for emoji in added:
                        if not await puppet.send_reaction(
                            b.irc_channel, emoji, irc_msgid, unreact=False,
                        ):
                            native = False
                            break
                    if native:
                        for emoji in removed:
                            await puppet.send_reaction(
                                b.irc_channel, emoji, irc_msgid, unreact=True,
                            )
                        continue
                    # TAGMSG unavailable: attributed text from the puppet
                    for emoji in added:
                        text = self._format_reaction_text(b, sid_ref, emoji)
                        await puppet.send_message(
                            b.irc_channel, text, reply_to=irc_msgid,
                        )
                    continue
                for emoji in added:
                    text = self._format_reaction_text(b, sid_ref, emoji)
                    await self._send_irc_line(
                        irc_client, irc_cfg, b.irc_channel, nick, text, b,
                        reply_to=irc_msgid,
                    )

        return handler

    async def _relay_action_to_irc(
        self,
        irc_client: IRCClient,
        irc_cfg: IRCConfig,
        channel: str,
        msg: XMPPMessage,
        b: BridgeMapping,
        xmpp_name: str | None = None,
    ) -> IRCClient:
        irc_reply_to = self._irc_reply_to_for_xmpp(xmpp_name, msg)
        puppet = await self._maybe_puppet(irc_cfg, irc_client, channel, msg.nick)
        if puppet is not None:
            await puppet.send_action(channel, msg.body, reply_to=irc_reply_to)
            return puppet
        if self._wants_relaymsg(irc_cfg, irc_client, channel):
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
        return irc_client
