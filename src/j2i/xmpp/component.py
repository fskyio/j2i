from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Callable, Awaitable
from xml.etree.ElementTree import SubElement

import slixmpp

from j2i.xmpp.avatar import Avatar
from j2i.xmpp.client import (
    XMPPMessage, MessageCallback, SelfMessageCallback, TypingCallback,
    ReactionCallback, OccupantCallback, _NS_REACTIONS, _NS_HINTS, _NS_MENTIONS,
    configure_software_identity, extract_stanza_id, incoming_xmpp_message,
)
from j2i.xmpp.mentions import Mention, attach_mentions
from j2i.xmpp.presence import (
    OccupantEvent, OccupantIdStore, extract_occupant_id, muc_user_info,
    occupant_event_from_presence,
)

ReconnectedCallback = Callable[[], Awaitable[None]]
# muc_jid, puppet_jid, irc_nick, reason, actor
PuppetBannedCallback = Callable[
    [str, str, str, str | None, str | None], Awaitable[None]
]

log = logging.getLogger(__name__)

_NS_MUC_USER = "http://jabber.org/protocol/muc#user"


def _muc_user_info(
    xml,
) -> tuple[set[str], str | None, str | None]:
    """Parse muc#user status codes, ban reason, and actor from a presence XML."""
    codes, reason, actor, _ = muc_user_info(xml)
    return codes, reason, actor


def _collision_nick(nick: str, puppet_jid: str) -> str:
    """Return a deterministic fallback MUC nick for when the preferred nick is taken.

    Uses a short hash of the puppet JID so the same IRC user always gets the
    same fallback, and it cannot be pre-empted by someone choosing that name.
    """
    h = hashlib.sha256(puppet_jid.encode()).hexdigest()[:7]
    return f"{nick} [{h}]"


class XMPPComponent:
    """
    XMPP component (XEP-0114) that manages IRC puppet JIDs in MUCs.

    The component connects using the Jabber Component Protocol.  A "master"
    JID (bridge@<domain>) joins every bridged MUC so the component can receive
    groupchat messages.  IRC users get puppet JIDs (<nick>.<alias>@<domain>)
    that join/leave MUCs as IRC users join/part/quit.
    """

    def __init__(
        self,
        component_domain: str,
        password: str,
        component_host: str = "localhost",
        component_port: int = 5347,
        nick: str = "IRC Bridge",
        avatar: Avatar | None = None,
        hide_version: bool = False,
        hide_os: bool = False,
    ) -> None:
        self.nick = nick
        self._avatar = avatar
        self._domain = component_domain
        # The master JID that sits in every bridged MUC to receive messages
        self._master_jid = f"bridge@{component_domain}"
        self._mucs: list[str] = []
        self._connected = asyncio.Event()
        self._reconnecting = False
        self._stopping = False

        # muc_jid.lower() -> {puppet_jid -> actual_muc_nick}
        # The actual nick may differ from the IRC nick after a collision.
        self._puppet_nicks: dict[str, dict[str, str]] = {}
        # muc_jid.lower() -> {puppet_jid -> original IRC nick}
        # Preserved across MUC nick collisions so bans can target the real nick.
        self._puppet_irc_nicks: dict[str, dict[str, str]] = {}
        self._occupant_ids = OccupantIdStore()

        # Same callback interface as XMPPClient
        self.on_message: MessageCallback | None = None
        self.on_self_message: SelfMessageCallback | None = None
        self.on_typing: TypingCallback | None = None
        self.on_reaction: ReactionCallback | None = None
        self.on_reconnected: ReconnectedCallback | None = None
        self.on_puppet_banned: PuppetBannedCallback | None = None
        self.on_occupant: OccupantCallback | None = None

        self._xmpp = slixmpp.ComponentXMPP(
            component_domain, password, component_host, component_port
        )
        self._xmpp.register_plugin("xep_0045")
        self._xmpp.register_plugin("xep_0054")   # vcard-temp (serves avatars)
        self._xmpp.register_plugin("xep_0085")
        self._xmpp.register_plugin("xep_0199")
        self._xmpp.register_plugin("xep_0308")
        self._xmpp.register_plugin("xep_0421")
        self._xmpp.register_plugin("xep_0444")
        self._xmpp.register_plugin("xep_0461")
        configure_software_identity(
            self._xmpp,
            hide_version=hide_version,
            hide_os=hide_os,
            identity_category="gateway",
            identity_type="irc",
        )
        self._xmpp["xep_0030"].add_feature(_NS_MENTIONS)

        self._xmpp.add_event_handler("session_start", self._on_session_start)
        self._xmpp.add_event_handler("disconnected", self._on_disconnected)
        self._xmpp.add_event_handler(
            "groupchat_message", self._on_groupchat_message
        )
        self._xmpp.add_event_handler(
            "chatstate_composing", self._on_chatstate_composing
        )
        self._xmpp.add_event_handler("chatstate_paused", self._on_chatstate_done)
        self._xmpp.add_event_handler("chatstate_active", self._on_chatstate_done)
        self._xmpp.add_event_handler("reactions", self._on_reactions)

        # Intercept presence errors via an input filter.  slixmpp's normal
        # event dispatch for "presence_error" is unreliable in component mode
        # (the basexmpp._handle_presence handler silently short-circuits for
        # MUC room JIDs and xep_0045 only fires muc::room::presence-error for
        # presences that include an muc_join element, which conflict errors
        # don't).  The filter runs after _build_stanza so we get a real
        # Presence object, before any handler can suppress it.
        self._xmpp.add_filter("in", self._filter_presence_errors)

        # Stamp the XEP-0153 photo hash onto outgoing MUC presence.  In Phase 1
        # only the master JID carries an avatar; Phase 2 extends _avatar_for to
        # resolve per-puppet avatars.  Components don't get xep_0153's own
        # presence filter for free, so we do it explicitly here.
        self._xmpp.add_filter("out", self._stamp_avatar)

    async def connect(self) -> None:
        self._xmpp.connect()
        await self._connected.wait()

    def add_muc(self, muc_jid: str) -> None:
        self._mucs.append(muc_jid)

    def occupant_id(self, muc_jid: str, nick: str) -> str | None:
        """XEP-0421 occupant id if the room advertised support and we have one."""
        return self._occupant_ids.get(muc_jid, nick)

    def _remember_occupant_id(self, muc_jid: str, nick: str, xml) -> None:
        if nick:
            self._occupant_ids.remember(muc_jid, nick, extract_occupant_id(xml))

    async def _probe_occupant_ids(self, muc_jid: str) -> None:
        try:
            supported = await self._xmpp["xep_0421"].has_feature(
                slixmpp.JID(muc_jid), ifrom=self._master_jid,
            )
        except Exception as e:
            log.debug("Occupant-id disco for %s failed: %s", muc_jid, e)
            supported = False
        self._occupant_ids.set_supported(muc_jid, supported)
        if supported:
            log.debug("MUC %s advertises XEP-0421 occupant ids", muc_jid)

    def is_puppet_nick(self, muc_jid: str, nick: str) -> bool:
        """Return True if nick is an IRC puppet in the given MUC."""
        return nick in self._puppet_nicks.get(muc_jid.lower(), {}).values()

    # ------------------------------------------------------------------
    # Avatars (XEP-0153)
    # ------------------------------------------------------------------

    def _avatar_for(self, jid_bare: str) -> Avatar | None:
        """Resolve the avatar for an outgoing sender JID.

        Phase 1: only the master JID has an avatar.  Phase 2 will look up a
        per-puppet avatar map keyed by puppet JID here.
        """
        if jid_bare == self._master_jid:
            return self._avatar
        return None

    async def _set_vcard(self, jid_bare: str, avatar: Avatar) -> None:
        """Store a vcard-temp carrying the avatar so xep_0054 can serve it."""
        vcard = self._xmpp["xep_0054"].make_vcard()
        vcard["PHOTO"]["TYPE"] = avatar.mime
        vcard["PHOTO"]["BINVAL"] = avatar.data
        await self._xmpp["xep_0054"].api["set_vcard"](
            jid=slixmpp.JID(jid_bare), args=vcard
        )

    def _stamp_avatar(self, stanza: object) -> object:
        """Outgoing filter: add the photo hash to available presence."""
        if not isinstance(stanza, slixmpp.Presence):
            return stanza
        if stanza["type"] not in ("", "available"):
            return stanza
        avatar = self._avatar_for(str(stanza["from"].bare))
        if avatar is None:
            return stanza
        if stanza.xml.find(f"{{{'vcard-temp:x:update'}}}x") is None:
            stanza.xml.append(avatar.photo_update_element())
        return stanza

    # ------------------------------------------------------------------
    # Master bot sends (used for system messages; IRC→XMPP uses puppets)
    # ------------------------------------------------------------------

    async def send_message(self, muc_jid: str, text: str) -> str:
        msg = self._xmpp.make_message(
            mto=muc_jid,
            mfrom=self._master_jid,
            mbody=text,
            mtype="groupchat",
        )
        msg_id = msg["id"]
        msg.send()
        return msg_id

    async def send_action(self, muc_jid: str, text: str) -> str:
        return await self.send_message(muc_jid, f"/me {text}")

    async def send_typing(self, muc_jid: str, composing: bool) -> None:
        msg = self._xmpp.make_message(
            mto=muc_jid,
            mfrom=self._master_jid,
            mtype="groupchat",
        )
        msg["chat_state"] = "composing" if composing else "active"
        msg.send()

    # ------------------------------------------------------------------
    # Puppet management
    # ------------------------------------------------------------------

    async def join_puppet(
        self, muc_jid: str, puppet_jid: str, nick: str
    ) -> None:
        """Send a MUC join presence for a puppet JID (fire-and-forget).

        Avoids join_muc_wait to prevent xep_0045 internal state corruption
        when multiple puppets join simultaneously or when a nick conflict
        causes a PresenceError mid-join.  Conflicts are handled asynchronously
        by _on_presence_error.
        """
        muc_key = muc_jid.lower()
        if puppet_jid in self._puppet_nicks.get(muc_key, {}):
            return
        stanza = self._xmpp["xep_0045"].make_join_stanza(
            muc_jid, nick,
            maxstanzas=0,
            presence_options={"pfrom": puppet_jid},
        )
        stanza.send()
        # Optimistically track the nick; _on_presence_error handles conflict
        self._track_puppet(muc_key, puppet_jid, irc_nick=nick, muc_nick=nick)
        log.debug("Puppet %s joining %s as %r", puppet_jid, muc_jid, nick)

    def _track_puppet(
        self, muc_key: str, puppet_jid: str, irc_nick: str, muc_nick: str
    ) -> None:
        self._puppet_nicks.setdefault(muc_key, {})[puppet_jid] = muc_nick
        self._puppet_irc_nicks.setdefault(muc_key, {})[puppet_jid] = irc_nick

    def _untrack_puppet(self, muc_key: str, puppet_jid: str) -> str | None:
        """Drop a puppet from tracking. Returns the original IRC nick, if known."""
        nicks = self._puppet_nicks.get(muc_key)
        muc_nick = None
        if nicks is not None:
            muc_nick = nicks.pop(puppet_jid, None)
        if muc_nick:
            self._occupant_ids.forget(muc_key, muc_nick)
        irc_nicks = self._puppet_irc_nicks.get(muc_key)
        if irc_nicks is None:
            return None
        return irc_nicks.pop(puppet_jid, None)

    async def _voice_puppet(self, muc_jid: str, nick: str) -> None:
        """Grant voice (role=participant) to a puppet in a MUC.

        Requires the master bot to be a moderator/admin/owner in the MUC.
        Silently ignored if the bot lacks permission.
        """
        try:
            await self._xmpp["xep_0045"].set_role(
                muc_jid, nick, "participant",
                ifrom=self._master_jid,
            )
            log.debug("Granted voice to %r in %s", nick, muc_jid)
        except Exception as e:
            log.debug("Could not grant voice to %r in %s: %s", nick, muc_jid, e)

    async def part_puppet(
        self, muc_jid: str, puppet_jid: str, nick: str
    ) -> None:
        """Remove an IRC user's puppet JID from a MUC."""
        muc_key = muc_jid.lower()
        actual_nick = self._puppet_nicks.get(muc_key, {}).get(puppet_jid)
        if actual_nick is None:
            return
        try:
            pres = self._xmpp.make_presence(
                pto=f"{muc_jid}/{actual_nick}",
                pfrom=puppet_jid,
                ptype="unavailable",
            )
            pres.send()
            self._untrack_puppet(muc_key, puppet_jid)
            log.debug("Puppet %s left %s", puppet_jid, muc_jid)
        except Exception as e:
            log.warning(
                "Failed to remove puppet %s from %s: %s", puppet_jid, muc_jid, e
            )

    def _ensure_puppet_joined(self, muc_jid: str, puppet_jid: str) -> bool:
        """Return True if puppet is tracked in the MUC. Log warning if not."""
        muc_key = muc_jid.lower()
        if puppet_jid in self._puppet_nicks.get(muc_key, {}):
            return True
        log.warning(
            "Puppet %s not in %s, dropping message", puppet_jid, muc_jid
        )
        return False

    async def send_puppet_message(
        self,
        muc_jid: str,
        puppet_jid: str,
        text: str,
        mentions: list[Mention] | None = None,
    ) -> str:
        """Send a groupchat message from a puppet JID. Returns client msg_id."""
        if not self._ensure_puppet_joined(muc_jid, puppet_jid):
            return ""
        msg = self._xmpp.make_message(
            mto=muc_jid,
            mfrom=puppet_jid,
            mbody=text,
            mtype="groupchat",
        )
        if mentions:
            attach_mentions(msg, mentions)
        msg["chat_state"] = "active"
        msg_id = msg["id"]
        msg.send()
        return msg_id

    async def send_puppet_action(
        self, muc_jid: str, puppet_jid: str, text: str
    ) -> str:
        """Send a /me action from a puppet JID (active chatstate included)."""
        return await self.send_puppet_message(muc_jid, puppet_jid, f"/me {text}")

    async def send_puppet_reply(
        self,
        muc_jid: str,
        puppet_jid: str,
        text: str,
        reply_to_id: str,
        reply_to: str | None = None,
        mentions: list[Mention] | None = None,
    ) -> str:
        """Send a groupchat reply (XEP-0461) from a puppet JID. Returns client msg_id."""
        if not self._ensure_puppet_joined(muc_jid, puppet_jid):
            return ""
        msg = self._xmpp.make_message(
            mto=muc_jid,
            mfrom=puppet_jid,
            mbody=text,
            mtype="groupchat",
        )
        msg["reply"]["id"] = reply_to_id
        if reply_to:
            msg["reply"]["to"] = reply_to
        if mentions:
            attach_mentions(msg, mentions)
        msg["chat_state"] = "active"
        msg_id = msg["id"]
        msg.send()
        return msg_id

    async def set_puppet_away(
        self, muc_jid: str, puppet_jid: str, nick: str, reason: str | None
    ) -> None:
        """Update a puppet's MUC presence to away or back to available."""
        muc_key = muc_jid.lower()
        actual_nick = self._puppet_nicks.get(muc_key, {}).get(puppet_jid)
        if actual_nick is None:
            return
        pres = self._xmpp.make_presence(
            pto=f"{muc_jid}/{actual_nick}",
            pfrom=puppet_jid,
            pshow="xa" if reason else None,
            pstatus=reason or "",
        )
        pres.send()

    async def send_puppet_typing(
        self, muc_jid: str, puppet_jid: str, composing: bool
    ) -> None:
        """Send a typing indicator from a puppet JID."""
        msg = self._xmpp.make_message(
            mto=muc_jid,
            mfrom=puppet_jid,
            mtype="groupchat",
        )
        msg["chat_state"] = "composing" if composing else "active"
        msg.send()

    async def send_reaction(
        self, muc_jid: str, stanza_id_ref: str, emojis: frozenset[str]
    ) -> None:
        """Send an XEP-0444 reaction from the master bot JID."""
        msg = self._xmpp.make_message(
            mto=muc_jid, mfrom=self._master_jid, mtype="groupchat"
        )
        reactions_el = SubElement(msg.xml, f"{{{_NS_REACTIONS}}}reactions")
        reactions_el.set("id", stanza_id_ref)
        for emoji in sorted(emojis):
            SubElement(reactions_el, f"{{{_NS_REACTIONS}}}reaction").text = emoji
        SubElement(msg.xml, f"{{{_NS_HINTS}}}store")
        msg.send()

    async def send_puppet_reaction(
        self, muc_jid: str, puppet_jid: str, stanza_id_ref: str, emojis: frozenset[str]
    ) -> None:
        """Send an XEP-0444 reaction from a puppet JID."""
        if not self._ensure_puppet_joined(muc_jid, puppet_jid):
            return
        msg = self._xmpp.make_message(
            mto=muc_jid, mfrom=puppet_jid, mtype="groupchat"
        )
        reactions_el = SubElement(msg.xml, f"{{{_NS_REACTIONS}}}reactions")
        reactions_el.set("id", stanza_id_ref)
        for emoji in sorted(emojis):
            SubElement(reactions_el, f"{{{_NS_REACTIONS}}}reaction").text = emoji
        SubElement(msg.xml, f"{{{_NS_HINTS}}}store")
        msg.send()

    # ------------------------------------------------------------------
    # slixmpp event handlers
    # ------------------------------------------------------------------

    async def _on_session_start(self, _event: dict) -> None:
        # Store the master's vCard so xep_0054 answers inbound vcard-temp gets
        # (in component mode the plugin serves gets straight from this cache).
        if self._avatar is not None:
            await self._set_vcard(self._master_jid, self._avatar)

        muc = self._xmpp["xep_0045"]
        for room in self._mucs:
            try:
                await muc.join_muc_wait(
                    room, self.nick,
                    presence_options={"pfrom": self._master_jid},
                    maxstanzas=0,
                )
                log.info("Component master joined XMPP MUC: %s", room)
                await self._probe_occupant_ids(room)
            except Exception as e:
                log.warning("Failed to join MUC %s: %s", room, e)
        is_reconnect = self._reconnecting
        self._reconnecting = False
        self._connected.set()
        if is_reconnect and self.on_reconnected:
            asyncio.ensure_future(self.on_reconnected())

    async def _on_disconnected(self, _event: dict) -> None:
        if self._stopping:
            return
        log.warning("XMPP component disconnected, reconnecting...")
        self._connected.clear()
        self._puppet_nicks.clear()
        self._puppet_irc_nicks.clear()
        self._occupant_ids.clear()
        self._reconnecting = True
        await asyncio.sleep(2)
        self._xmpp.connect()

    def _filter_presence_errors(self, stanza) -> object:
        """Input filter: puppet conflicts, kicks/bans, master kicks, auto-voice."""
        if not isinstance(stanza, slixmpp.Presence):
            return stanza

        to_bare = str(stanza["to"].bare)

        # Puppet nick conflict
        if stanza["type"] == "error" and to_bare != self._master_jid:
            asyncio.ensure_future(self._on_presence_error(stanza))

        # Master kicked or banned from MUC, or a real occupant left/renamed
        elif stanza["type"] == "unavailable" and to_bare == self._master_jid:
            event = occupant_event_from_presence(stanza, self.nick)
            if event is not None and event.is_self:
                codes, _, _ = _muc_user_info(stanza.xml)
                muc_jid = str(stanza["from"].bare)
                if "307" in codes:
                    log.warning(
                        "Master JID kicked from %s, will rejoin in 5s", muc_jid
                    )
                    asyncio.ensure_future(self._rejoin_after_kick(muc_jid))
                elif "301" in codes:
                    log.warning("Master JID banned from %s, not rejoining", muc_jid)
            elif event is not None:
                self._occupant_ids.apply_event(event)
                self._emit_occupant(event)

        # Puppet kicked or banned from MUC (presence addressed to the puppet)
        elif stanza["type"] == "unavailable" and to_bare != self._master_jid:
            self._on_puppet_unavailable(stanza)

        # Occupant available (and auto-voice for puppets)
        elif stanza["type"] in ("", "available") and to_bare == self._master_jid:
            nick = stanza["from"].resource
            muc_jid = str(stanza["from"].bare)
            muc_key = muc_jid.lower()
            if nick and self._is_puppet_echo(muc_key, nick):
                self._remember_occupant_id(muc_jid, nick, stanza.xml)
                muc_x = stanza.xml.find(f"{{{_NS_MUC_USER}}}x")
                if muc_x is not None:
                    item = muc_x.find(f"{{{_NS_MUC_USER}}}item")
                    if item is not None and item.get("role") == "visitor":
                        asyncio.ensure_future(self._voice_puppet(muc_jid, nick))
            else:
                event = occupant_event_from_presence(stanza, self.nick)
                if event is not None and not event.is_self:
                    self._occupant_ids.apply_event(event)
                    self._emit_occupant(event)

        return stanza

    def _emit_occupant(self, event: OccupantEvent) -> None:
        if self._is_puppet_echo(event.muc_jid.lower(), event.nick):
            return
        if event.new_nick and self._is_puppet_echo(
            event.muc_jid.lower(), event.new_nick
        ):
            return
        if self.on_occupant:
            asyncio.ensure_future(self.on_occupant(event))

    def _on_puppet_unavailable(self, pres: slixmpp.Presence) -> None:
        """Handle a puppet's own unavailable presence (kick/ban/leave echo)."""
        puppet_jid = str(pres["to"].bare)
        muc_jid = str(pres["from"].bare)
        muc_key = muc_jid.lower()
        if puppet_jid not in self._puppet_nicks.get(muc_key, {}):
            return

        codes, reason, actor = _muc_user_info(pres.xml)
        if "301" in codes:
            irc_nick = self._untrack_puppet(muc_key, puppet_jid)
            log.warning(
                "Puppet %s (%s) banned from %s", puppet_jid, irc_nick, muc_jid
            )
            if irc_nick and self.on_puppet_banned:
                asyncio.ensure_future(
                    self.on_puppet_banned(
                        muc_jid, puppet_jid, irc_nick, reason, actor
                    )
                )
        elif "307" in codes:
            irc_nick = self._untrack_puppet(muc_key, puppet_jid)
            log.info(
                "Puppet %s (%s) kicked from %s, not rejoining",
                puppet_jid, irc_nick, muc_jid,
            )

    async def _rejoin_after_kick(self, muc_jid: str) -> None:
        await asyncio.sleep(5)
        try:
            await self._xmpp["xep_0045"].join_muc_wait(
                muc_jid, self.nick,
                presence_options={"pfrom": self._master_jid},
                maxstanzas=0,
            )
            log.info("Rejoined MUC %s after kick", muc_jid)
            await self._probe_occupant_ids(muc_jid)
        except Exception as e:
            log.warning("Failed to rejoin MUC %s: %s", muc_jid, e)

    async def _on_presence_error(self, pres: slixmpp.Presence) -> None:
        """Handle MUC presence errors for puppets."""
        puppet_jid = str(pres["to"].bare)
        if puppet_jid == self._master_jid:
            return

        muc_jid = str(pres["from"].bare)
        muc_key = muc_jid.lower()

        tried_nick = self._puppet_nicks.get(muc_key, {}).get(puppet_jid)
        if tried_nick is None:
            return

        # Nick conflict - retry with a hash-based fallback nick
        is_conflict = pres.xml.find(
            ".//{urn:ietf:params:xml:ns:xmpp-stanzas}conflict"
        ) is not None

        if is_conflict:
            localpart = puppet_jid.split("@")[0]
            original_nick = localpart.rsplit(".", 1)[0]
            hash_nick = _collision_nick(original_nick, puppet_jid)

            if tried_nick == hash_nick:
                log.warning(
                    "Could not join puppet %s to %s: both nick attempts failed",
                    puppet_jid, muc_jid,
                )
                self._untrack_puppet(muc_key, puppet_jid)
                return

            log.info(
                "Nick conflict for %r in %s, retrying as %r",
                tried_nick, muc_jid, hash_nick,
            )
            self._puppet_nicks[muc_key][puppet_jid] = hash_nick
            self._occupant_ids.forget(muc_key, tried_nick)
            stanza = self._xmpp["xep_0045"].make_join_stanza(
                muc_jid, hash_nick,
                maxstanzas=0,
                presence_options={"pfrom": puppet_jid},
            )
            stanza.send()
            return

        # Any other error (forbidden, registration-required, etc.) -
        # clean up the optimistic tracking so the puppet isn't a ghost
        err = pres.xml.find("{jabber:client}error")
        err_type = err.get("type", "unknown") if err is not None else "unknown"
        log.warning(
            "Puppet %s failed to join %s (nick %r): %s",
            puppet_jid, muc_jid, tried_nick, err_type,
        )
        self._untrack_puppet(muc_key, puppet_jid)

    def _is_puppet_echo(self, muc_key: str, nick: str) -> bool:
        return nick in self._puppet_nicks.get(muc_key, {}).values()

    async def _on_groupchat_message(self, msg: slixmpp.Message) -> None:
        # The MUC delivers a copy to every JID in the room (master + all puppets).
        # Only process the copy addressed to the master JID to avoid duplicates.
        if str(msg["to"].bare) != self._master_jid:
            return

        nick = msg["mucnick"]
        muc_jid = str(msg["from"].bare)
        muc_key = muc_jid.lower()
        self._remember_occupant_id(muc_jid, nick, msg.xml)

        # Master bot echo - ignore
        if nick == self.nick:
            return

        # Puppet echo - capture stanza-id for reply threading, then drop
        if self._is_puppet_echo(muc_key, nick):
            if self.on_self_message:
                client_id = msg["id"]
                stanza_id, stanza_id_by = extract_stanza_id(msg)
                if client_id and stanza_id:
                    await self.on_self_message(
                        muc_jid, client_id, stanza_id, stanza_id_by
                    )
            return

        xmpp_msg = incoming_xmpp_message(msg)
        if xmpp_msg is None:
            return

        if self.on_message:
            await self.on_message(xmpp_msg)

    async def _on_reactions(self, msg: slixmpp.Message) -> None:
        """slixmpp xep_0444 fires a dedicated 'reactions' event for reaction stanzas."""
        if msg["type"] != "groupchat":
            return
        if str(msg["to"].bare) != self._master_jid:
            return
        nick = msg["mucnick"]
        muc_jid = str(msg["from"].bare)
        muc_key = muc_jid.lower()
        if nick == self.nick or self._is_puppet_echo(muc_key, nick):
            return
        if not self.on_reaction:
            return
        reactions = msg["reactions"]
        ref = reactions["id"]
        if not ref:
            return
        emojis = frozenset(reactions.get_values(all_chars=True))
        await self.on_reaction(muc_jid, nick, ref, emojis)

    async def _on_chatstate_composing(self, msg: slixmpp.Message) -> None:
        if msg["type"] != "groupchat":
            return
        if str(msg["to"].bare) != self._master_jid:
            return
        nick = msg["mucnick"]
        muc_jid = str(msg["from"].bare)
        muc_key = muc_jid.lower()
        if nick == self.nick or self._is_puppet_echo(muc_key, nick):
            return
        if self.on_typing:
            await self.on_typing(muc_jid, nick, True)

    async def _on_chatstate_done(self, msg: slixmpp.Message) -> None:
        if msg["type"] != "groupchat":
            return
        if str(msg["to"].bare) != self._master_jid:
            return
        nick = msg["mucnick"]
        muc_jid = str(msg["from"].bare)
        muc_key = muc_jid.lower()
        if nick == self.nick or self._is_puppet_echo(muc_key, nick):
            return
        if self.on_typing:
            await self.on_typing(muc_jid, nick, False)

    def stop(self) -> None:
        self._stopping = True
        self._xmpp.disconnect()

    async def disconnect(self) -> None:
        self._xmpp.disconnect()
