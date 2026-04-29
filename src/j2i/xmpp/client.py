from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from importlib.metadata import version as _pkg_version
from typing import Callable, Awaitable
from xml.etree.ElementTree import SubElement

import slixmpp

log = logging.getLogger(__name__)


@dataclass
class XMPPMessage:
    muc_jid: str
    nick: str
    body: str
    is_action: bool = False
    # Smart replies
    reply_to_nick: str | None = None
    reply_to_id: str | None = None
    # Smart edits: this message is a correction of a previous one
    is_correction: bool = False
    # Server-assigned stanza-id (XEP-0359), used for reply threading
    stanza_id: str | None = None


MessageCallback = Callable[[XMPPMessage], Awaitable[None]]
# Called when our own message is reflected back: (client_id, server_stanza_id)
SelfMessageCallback = Callable[[str, str], Awaitable[None]]
# Typing: (muc_jid, nick, is_typing)
TypingCallback = Callable[[str, str, bool], Awaitable[None]]
# Reactions: (muc_jid, nick, stanza_id_ref, emojis)
ReactionCallback = Callable[[str, str, str, "frozenset[str]"], Awaitable[None]]

_NS_REACTIONS = "urn:xmpp:reactions:0"
_NS_HINTS = "urn:xmpp:hints"


class XMPPClient:
    def __init__(
        self,
        jid: str,
        password: str,
        nick: str = "IRC Bridge",
    ) -> None:
        self.nick = nick
        self.on_message: MessageCallback | None = None
        self.on_self_message: SelfMessageCallback | None = None
        self.on_typing: TypingCallback | None = None
        self.on_reaction: ReactionCallback | None = None
        self._mucs: list[str] = []
        self._connected = asyncio.Event()
        self._stopping = False

        self._xmpp = slixmpp.ClientXMPP(jid, password)
        try:
            _ver = _pkg_version("j2i")
            self._xmpp.requested_jid.resource = f"j2i {_ver}"
        except Exception:
            _ver = ""
            self._xmpp.requested_jid.resource = "j2i"
        self._xmpp.register_plugin("xep_0045")   # MUC
        self._xmpp.register_plugin("xep_0085")   # Chat State Notifications
        self._xmpp.register_plugin("xep_0199")   # Ping
        self._xmpp.register_plugin("xep_0308")   # Last Message Correction
        self._xmpp.register_plugin("xep_0444")   # Message Reactions
        self._xmpp.register_plugin("xep_0461")   # Message Replies
        # xep_0045 pulls in xep_0115 (Entity Capabilities) transitively;
        # override slixmpp defaults so clients see "j2i" not "Slixmpp x.y.z"
        self._xmpp["xep_0115"].caps_node = "https://telepath.im/projects/j2i"
        self._xmpp["xep_0030"].add_identity(
            category="client", itype="bot",
            name=f"j2i {_ver}".strip() if _ver else "j2i",
        )

        self._xmpp.add_event_handler("session_start", self._on_session_start)
        self._xmpp.add_event_handler("disconnected", self._on_disconnected)
        self._xmpp.add_event_handler(
            "groupchat_message", self._on_groupchat_message
        )
        self._xmpp.add_event_handler(
            "groupchat_presence", self._on_groupchat_presence
        )
        self._xmpp.add_event_handler(
            "chatstate_composing", self._on_chatstate_composing
        )
        self._xmpp.add_event_handler(
            "chatstate_paused", self._on_chatstate_done
        )
        self._xmpp.add_event_handler(
            "chatstate_active", self._on_chatstate_done
        )
        self._xmpp.add_event_handler("reactions", self._on_reactions)

    async def connect(self) -> None:
        self._xmpp.connect()
        await self._connected.wait()

    def add_muc(self, muc_jid: str) -> None:
        self._mucs.append(muc_jid)

    async def send_message(self, muc_jid: str, text: str) -> str:
        msg = self._xmpp.make_message(
            mto=muc_jid,
            mbody=text,
            mtype="groupchat",
        )
        msg_id = msg["id"]
        msg.send()
        return msg_id

    async def send_action(self, muc_jid: str, text: str) -> str:
        return await self.send_message(muc_jid, f"/me {text}")

    async def send_reply(
        self, muc_jid: str, text: str, reply_to_id: str, reply_to: str | None = None
    ) -> str:
        """Send a groupchat message as an XEP-0461 reply."""
        msg = self._xmpp.make_message(
            mto=muc_jid,
            mbody=text,
            mtype="groupchat",
        )
        msg["reply"]["id"] = reply_to_id
        if reply_to:
            msg["reply"]["to"] = reply_to
        msg_id = msg["id"]
        msg.send()
        return msg_id

    async def send_reaction(
        self, muc_jid: str, stanza_id_ref: str, emojis: frozenset[str]
    ) -> None:
        """Send an XEP-0444 reaction stanza to a MUC. Empty emojis = remove all."""
        msg = self._xmpp.make_message(mto=muc_jid, mtype="groupchat")
        reactions_el = SubElement(msg.xml, f"{{{_NS_REACTIONS}}}reactions")
        reactions_el.set("id", stanza_id_ref)
        for emoji in sorted(emojis):
            SubElement(reactions_el, f"{{{_NS_REACTIONS}}}reaction").text = emoji
        SubElement(msg.xml, f"{{{_NS_HINTS}}}store")
        msg.send()

    async def send_typing(self, muc_jid: str, composing: bool) -> None:
        msg = self._xmpp.make_message(
            mto=muc_jid,
            mtype="groupchat",
        )
        msg["chat_state"] = "composing" if composing else "active"
        msg.send()

    async def _on_session_start(self, _event: dict) -> None:
        await self._xmpp.get_roster()
        self._xmpp.send_presence()

        muc = self._xmpp.plugin["xep_0045"]
        for room in self._mucs:
            try:
                await muc.join_muc_wait(room, self.nick, maxstanzas=0)
                log.info("Joined XMPP MUC: %s", room)
            except Exception as e:
                log.warning("Failed to join MUC %s: %s", room, e)

        self._connected.set()

    async def _on_disconnected(self, _event: dict) -> None:
        if self._stopping:
            return
        log.warning("XMPP disconnected, reconnecting...")
        self._connected.clear()
        await asyncio.sleep(2)
        self._xmpp.connect()

    async def _on_groupchat_presence(self, pres: slixmpp.Presence) -> None:
        if pres["type"] != "unavailable":
            return
        muc_x = pres.xml.find("{http://jabber.org/protocol/muc#user}x")
        if muc_x is None:
            return
        codes = {
            s.get("code")
            for s in muc_x.findall("{http://jabber.org/protocol/muc#user}status")
        }
        # code 110 = our own presence; code 307 = kicked; code 301 = banned
        if "110" not in codes:
            return
        muc_jid = str(pres["from"].bare)
        if "307" in codes:
            log.warning("Kicked from MUC %s, will rejoin in 5s", muc_jid)
            asyncio.ensure_future(self._rejoin_muc(muc_jid))
        elif "301" in codes:
            log.warning("Banned from MUC %s, not rejoining", muc_jid)

    async def _rejoin_muc(self, muc_jid: str) -> None:
        await asyncio.sleep(5)
        try:
            await self._xmpp["xep_0045"].join_muc_wait(
                muc_jid, self.nick, maxstanzas=0
            )
            log.info("Rejoined MUC %s after kick", muc_jid)
        except Exception as e:
            log.warning("Failed to rejoin MUC %s: %s", muc_jid, e)

    async def _on_groupchat_message(self, msg: slixmpp.Message) -> None:
        nick = msg["mucnick"]
        if nick == self.nick:
            # Our own message reflected back - grab the server-assigned
            # stanza-id and map it to our client-generated id
            if self.on_self_message:
                client_id = msg["id"]
                stanza_id = self._extract_stanza_id(msg)
                if client_id and stanza_id:
                    await self.on_self_message(client_id, stanza_id)
            return

        body = msg["body"]
        if not body:
            return

        muc_jid = str(msg["from"].bare)

        # Detect correction (XEP-0308)
        is_correction = bool(msg["replace"]["id"])

        # Detect reply (XEP-0461) and extract reply-to nick
        reply_to_nick = None
        reply_id = msg["reply"]["id"]
        log.debug("Incoming message id=%s, reply_to_id=%s", msg["id"], reply_id)
        if reply_id:
            # Strip the fallback quote from the body
            body = msg["reply"].strip_fallback_content() or body
            # The reply "to" attribute is the full JID of the replied-to message
            # The resource part of a MUC JID is the nick
            reply_to_jid = msg["reply"]["to"]
            if reply_to_jid:
                reply_to_nick = slixmpp.JID(reply_to_jid).resource

        is_action = body.startswith("/me ")
        if is_action:
            body = body[4:]

        xmpp_msg = XMPPMessage(
            muc_jid=muc_jid,
            nick=nick,
            body=body,
            is_action=is_action,
            reply_to_nick=reply_to_nick,
            reply_to_id=reply_id or None,
            is_correction=is_correction,
            stanza_id=self._extract_stanza_id(msg),
        )

        if self.on_message:
            await self.on_message(xmpp_msg)

    async def _on_reactions(self, msg: slixmpp.Message) -> None:
        """slixmpp xep_0444 fires a dedicated 'reactions' event for reaction stanzas."""
        if msg["type"] != "groupchat":
            return
        nick = msg["mucnick"]
        if nick == self.nick:
            return
        if not self.on_reaction:
            return
        muc_jid = str(msg["from"].bare)
        reactions = msg["reactions"]
        ref = reactions["id"]
        if not ref:
            return
        emojis = frozenset(reactions.get_values(all_chars=True))
        await self.on_reaction(muc_jid, nick, ref, emojis)

    async def _on_chatstate_composing(self, msg: slixmpp.Message) -> None:
        if msg["type"] != "groupchat":
            return
        nick = msg["mucnick"]
        if nick == self.nick:
            return
        muc_jid = str(msg["from"].bare)
        if self.on_typing:
            await self.on_typing(muc_jid, nick, True)

    async def _on_chatstate_done(self, msg: slixmpp.Message) -> None:
        if msg["type"] != "groupchat":
            return
        nick = msg["mucnick"]
        if nick == self.nick:
            return
        muc_jid = str(msg["from"].bare)
        if self.on_typing:
            await self.on_typing(muc_jid, nick, False)

    @staticmethod
    def _extract_stanza_id(msg: slixmpp.Message) -> str | None:
        el = msg.xml.find("{urn:xmpp:sid:0}stanza-id")
        if el is not None:
            return el.get("id")
        return None

    def stop(self) -> None:
        self._stopping = True
        self._xmpp.disconnect()

    async def disconnect(self) -> None:
        self._xmpp.disconnect()
