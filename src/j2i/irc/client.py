from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from dataclasses import dataclass, field
from typing import Callable, Awaitable

log = logging.getLogger(__name__)


@dataclass
class IRCMessage:
    channel: str
    nick: str
    text: str
    is_action: bool = False
    # IRCv3 message-ids
    msgid: str | None = None
    # IRCv3 +reply client tag
    reply_to_msgid: str | None = None


# Callback types
MessageCallback = Callable[["IRCMessage"], Awaitable[None]]
ActionCallback = Callable[["IRCMessage"], Awaitable[None]]
TypingCallback = Callable[[str, str, bool], Awaitable[None]]  # channel, nick, is_typing
PresenceCallback = Callable[[str, str], Awaitable[None]]  # channel, nick
QuitCallback = Callable[[str, list[str]], Awaitable[None]]  # nick, channels
NickCallback = Callable[[str, str, list[str]], Awaitable[None]]  # old_nick, new_nick, channels
NamesCallback = Callable[[str, set[str]], Awaitable[None]]  # channel, nicks
AwayCallback = Callable[[str, list[str], str | None], Awaitable[None]]  # nick, channels, reason|None
SelfKickedCallback = Callable[[str], Awaitable[None]]  # channel
SelfMsgCallback = Callable[[str, str], Awaitable[None]]  # channel, msgid
ReactCallback = Callable[[str, str, str, "str | None", bool], Awaitable[None]]  # channel, nick, emoji, reply_to_msgid, is_unreact


@dataclass
class _InboundBatch:
    """State for an in-progress server-to-client batch."""
    batch_type: str
    target: str
    nick: str = ""
    msgid: str | None = None
    reply_to_msgid: str | None = None
    # (text, concat) tuples, where concat=True means no separator before this line
    lines: list[tuple[str, bool]] = field(default_factory=list)


@dataclass
class IRCClient:
    host: str
    port: int
    nick: str
    tls: bool = True
    sasl_password: str | None = None
    nickserv_password: str | None = None

    # Capabilities detected during negotiation
    has_relaymsg: bool = False
    relaymsg_separator: str = "/"
    has_message_tags: bool = False
    has_echo_message: bool = False
    has_batch: bool = False
    has_multiline: bool = False
    multiline_max_bytes: int = 0
    multiline_max_lines: int = 0
    # ISUPPORT tokens
    has_utf8only: bool = False
    bot_mode_char: str | None = None
    _has_sasl: bool = field(default=False, repr=False)
    _sasl_started: bool = field(default=False, repr=False)
    _sasl_done: bool = field(default=False, repr=False)
    # Accumulate caps across multi-line CAP LS before sending REQ
    _pending_caps: list[str] = field(default_factory=list, repr=False)
    _cap_ls_done: bool = field(default=False, repr=False)
    # Active inbound batches keyed by ref
    _inbound_batches: dict[str, "_InboundBatch"] = field(
        default_factory=dict, repr=False,
    )
    # Counter for outbound batch refs
    _batch_counter: int = field(default=0, repr=False)

    # Channels the client has joined and whether it has +o in each
    channels: dict[str, bool] = field(default_factory=dict)  # channel -> is_op

    # Member tracking per channel (populated via NAMES on join)
    _channel_members: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _pending_names: dict[str, set[str]] = field(default_factory=dict, repr=False)

    # Callbacks
    on_message: MessageCallback | None = None
    on_action: ActionCallback | None = None
    on_typing: TypingCallback | None = None
    on_disconnect: Callable[[], Awaitable[None]] | None = None
    on_user_join: PresenceCallback | None = None
    on_user_part: PresenceCallback | None = None
    on_user_quit: QuitCallback | None = None
    on_user_nick: NickCallback | None = None
    on_names_done: NamesCallback | None = None
    on_user_away: AwayCallback | None = None
    on_self_kicked: SelfKickedCallback | None = None
    on_self_message: SelfMsgCallback | None = None
    on_reaction: ReactCallback | None = None

    _reader: asyncio.StreamReader | None = field(default=None, repr=False)
    _writer: asyncio.StreamWriter | None = field(default=None, repr=False)
    _registered: bool = field(default=False, repr=False)
    _register_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )

    async def connect(self) -> None:
        # Reset state from any previous connection
        self._registered = False
        self._register_event.clear()
        self.has_relaymsg = False
        self.has_message_tags = False
        self.has_echo_message = False
        self.has_batch = False
        self.has_multiline = False
        self.multiline_max_bytes = 0
        self.multiline_max_lines = 0
        self.has_utf8only = False
        self.bot_mode_char = None
        self._has_sasl = False
        self._sasl_started = False
        self._sasl_done = False
        self._pending_caps.clear()
        self._cap_ls_done = False
        self.channels.clear()
        self._channel_members.clear()
        self._pending_names.clear()
        self._inbound_batches.clear()
        self._batch_counter = 0

        ssl_ctx = None
        if self.tls:
            ssl_ctx = ssl.create_default_context()

        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx,
        )
        log.info("Connected to %s:%d", self.host, self.port)

        # Start capability negotiation
        await self._send("CAP LS 302")
        await self._send(f"NICK {self.nick}")
        await self._send(f"USER {self.nick} 0 * :{self.nick}")

    async def run(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line_bytes = await self._reader.readline()
                if not line_bytes:
                    log.warning("Connection closed by server")
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                log.debug("<< %s", line)
                await self._handle(line)
        except (ConnectionResetError, OSError) as e:
            log.warning("IRC connection error: %s", e)
        finally:
            self._close_writer()
            if self.on_disconnect:
                await self.on_disconnect()

    def _close_writer(self) -> None:
        if self._writer:
            try:
                self._writer.close()
            except OSError:
                pass
            self._writer = None
        self._reader = None

    def get_members(self, channel: str) -> set[str]:
        return set(self._channel_members.get(channel.lower(), set()))

    async def join(self, channel: str) -> None:
        await self._register_event.wait()
        await self._send(f"JOIN {channel}")
        self.channels.setdefault(channel.lower(), False)

    async def request_names(self, channel: str) -> None:
        await self._send(f"NAMES {channel}")

    async def send_message(
        self, channel: str, text: str, reply_to: str | None = None
    ) -> None:
        tag_prefix = f"@+reply={reply_to} " if reply_to and self.has_message_tags else ""
        await self._send(f"{tag_prefix}PRIVMSG {channel} :{text}")

    async def send_action(self, channel: str, text: str) -> None:
        await self._send(f"PRIVMSG {channel} :\x01ACTION {text}\x01")

    async def send_relaymsg(
        self, channel: str, spoofed_nick: str, text: str,
        reply_to: str | None = None,
    ) -> None:
        nick = f"{spoofed_nick}{self.relaymsg_separator}xmpp"
        tag_prefix = f"@+reply={reply_to} " if reply_to and self.has_message_tags else ""
        await self._send(f"{tag_prefix}RELAYMSG {channel} {nick} :{text}")

    def can_relaymsg(self, channel: str) -> bool:
        return self.has_relaymsg and self.channels.get(channel.lower(), False)

    def can_multiline(self, lines: list[str]) -> bool:
        """Return True if the given lines fit the negotiated multiline limits."""
        if not (self.has_multiline and self.has_batch and self.has_message_tags):
            return False
        if len(lines) < 2:
            return False
        if self.multiline_max_lines and len(lines) > self.multiline_max_lines:
            return False
        if self.multiline_max_bytes:
            # \n separators between lines also count toward max-bytes
            total = sum(len(l.encode("utf-8")) for l in lines) + max(0, len(lines) - 1)
            if total > self.multiline_max_bytes:
                return False
        return True

    async def send_multiline_message(
        self, channel: str, lines: list[str], reply_to: str | None = None,
    ) -> None:
        """Send a multi-line message wrapped in a draft/multiline BATCH.

        Caller must have verified can_multiline(lines) first.
        """
        self._batch_counter += 1
        ref = f"j2i{self._batch_counter}"
        batch_tags = (
            f"@+reply={reply_to} " if reply_to and self.has_message_tags else ""
        )
        await self._send(f"{batch_tags}BATCH +{ref} draft/multiline {channel}")
        for line in lines:
            await self._send(f"@batch={ref} PRIVMSG {channel} :{line}")
        await self._send(f"BATCH -{ref}")

    async def send_typing(self, channel: str, active: bool) -> None:
        if not self.has_message_tags:
            return
        value = "active" if active else "done"
        await self._send(f"@+typing={value} TAGMSG {channel}")

    async def _send(self, line: str) -> None:
        if self._writer is None:
            log.warning("Cannot send, not connected: %s", line)
            return
        log.debug(">> %s", line)
        self._writer.write((line + "\r\n").encode("utf-8"))
        await self._writer.drain()

    async def _handle(self, raw: str) -> None:
        # Parse optional IRCv3 tags
        tags: dict[str, str] = {}
        rest = raw
        if rest.startswith("@"):
            tag_str, rest = rest.split(" ", 1)
            for pair in tag_str[1:].split(";"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    tags[k] = v
                else:
                    tags[pair] = ""

        # Parse prefix
        prefix = ""
        if rest.startswith(":"):
            prefix, rest = rest.split(" ", 1)
            prefix = prefix[1:]

        # Parse command and params
        if " :" in rest:
            head, trailing = rest.split(" :", 1)
            parts = head.split()
            command = parts[0].upper()
            params = parts[1:] + [trailing]
        else:
            parts = rest.split()
            command = parts[0].upper()
            params = parts[1:]

        await self._dispatch(tags, prefix, command, params)

    async def _dispatch(
        self,
        tags: dict[str, str],
        prefix: str,
        command: str,
        params: list[str],
    ) -> None:
        if command == "PING":
            await self._send(f"PONG :{params[0]}" if params else "PONG")

        elif command == "CAP":
            await self._handle_cap(params)

        elif command == "AUTHENTICATE":
            await self._handle_authenticate(params)

        elif command == "903":
            # RPL_SASLSUCCESS
            log.info("SASL authentication successful")
            self._sasl_done = True
            await self._send("CAP END")

        elif command in ("902", "904", "905"):
            # SASL auth failed
            log.error("SASL authentication failed (%s): %s", command, params)
            self._sasl_done = True
            await self._send("CAP END")

        elif command == "005":
            await self._handle_isupport(params)

        elif command == "001":
            # RPL_WELCOME - registration complete
            self._registered = True
            self._register_event.set()
            log.info("Registered as %s", self.nick)
            if self.nickserv_password and not self._sasl_done:
                await self._send(
                    f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}"
                )

        elif command == "MODE":
            await self._handle_mode(params)

        elif command == "JOIN":
            await self._handle_join(prefix, params)

        elif command == "PART":
            await self._handle_part(prefix, params)

        elif command == "QUIT":
            await self._handle_quit(prefix)

        elif command == "NICK":
            await self._handle_nick(prefix, params)

        elif command == "KICK":
            await self._handle_kick(params)

        elif command == "353":
            self._handle_names(params)

        elif command == "366":
            await self._handle_names_end(params)

        elif command == "AWAY":
            await self._handle_away(prefix, params)

        elif command == "PRIVMSG":
            await self._handle_privmsg(tags, prefix, params)

        elif command == "TAGMSG":
            await self._handle_tagmsg(tags, prefix, params)

        elif command == "BATCH":
            await self._handle_batch(tags, prefix, params)

    async def _handle_join(self, prefix: str, params: list[str]) -> None:
        if not params:
            return
        channel = params[0].lower()
        nick = prefix.split("!")[0] if "!" in prefix else prefix
        if nick == self.nick:
            # Our own join confirmed by server - request member list
            await self._send(f"NAMES {channel}")
        else:
            self._channel_members.setdefault(channel, set()).add(nick)
            if self.on_user_join:
                await self.on_user_join(channel, nick)

    async def _handle_part(self, prefix: str, params: list[str]) -> None:
        if not params:
            return
        channel = params[0].lower()
        nick = prefix.split("!")[0] if "!" in prefix else prefix
        if nick == self.nick:
            return
        self._channel_members.get(channel, set()).discard(nick)
        if self.on_user_part:
            await self.on_user_part(channel, nick)

    async def _handle_quit(self, prefix: str) -> None:
        nick = prefix.split("!")[0] if "!" in prefix else prefix
        if nick == self.nick:
            return
        channels_left = [
            ch for ch, members in self._channel_members.items()
            if nick in members
        ]
        for ch in channels_left:
            self._channel_members[ch].discard(nick)
        if channels_left and self.on_user_quit:
            await self.on_user_quit(nick, channels_left)

    async def _handle_nick(self, prefix: str, params: list[str]) -> None:
        if not params:
            return
        old_nick = prefix.split("!")[0] if "!" in prefix else prefix
        new_nick = params[0]
        if old_nick == self.nick:
            self.nick = new_nick
            return
        shared_channels = []
        for ch, members in self._channel_members.items():
            if old_nick in members:
                members.discard(old_nick)
                members.add(new_nick)
                shared_channels.append(ch)
        if shared_channels and self.on_user_nick:
            await self.on_user_nick(old_nick, new_nick, shared_channels)

    async def _handle_kick(self, params: list[str]) -> None:
        if len(params) < 2:
            return
        channel = params[0].lower()
        kicked_nick = params[1]
        if kicked_nick.lower() == self.nick.lower():
            self.channels.pop(channel, None)
            self._channel_members.pop(channel, None)
            if self.on_self_kicked:
                await self.on_self_kicked(channel)
            return
        self._channel_members.get(channel, set()).discard(kicked_nick)
        if self.on_user_part:
            await self.on_user_part(channel, kicked_nick)

    def _handle_names(self, params: list[str]) -> None:
        # 353: <client> <sym> <channel> :<nick list>
        if len(params) < 3:
            return
        channel = params[-2].lower()
        nicks_raw = params[-1]
        nicks: set[str] = set()
        for n in nicks_raw.split():
            # Detect our own op status from the prefix
            if n.startswith("@") and n.lstrip("@+~%&!").lower() == self.nick.lower():
                self.channels[channel] = True
                log.info("%s in %s: op=True (from NAMES)", self.nick, channel)
            nick = n.lstrip("@+~%&!")
            if nick:
                nicks.add(nick)
        self._pending_names.setdefault(channel, set()).update(nicks)

    async def _handle_names_end(self, params: list[str]) -> None:
        # 366: <client> <channel> :End of NAMES list
        if len(params) < 2:
            return
        channel = params[1].lower()
        members = self._pending_names.pop(channel, set())
        members.discard(self.nick)
        self._channel_members[channel] = members
        if self.on_names_done:
            await self.on_names_done(channel, members)

    async def _handle_away(self, prefix: str, params: list[str]) -> None:
        nick = prefix.split("!")[0] if "!" in prefix else prefix
        if nick == self.nick:
            return
        reason = params[0] if params else None  # None = back from away
        channels = [
            ch for ch, members in self._channel_members.items()
            if nick in members
        ]
        if channels and self.on_user_away:
            await self.on_user_away(nick, channels, reason)

    async def _handle_isupport(self, params: list[str]) -> None:
        # params: [our_nick, token1, token2, ..., ":are supported by this server"]
        for token in params[1:-1]:
            if "=" in token:
                key, value = token.split("=", 1)
            else:
                key, value = token, ""
            key = key.upper()
            if key == "UTF8ONLY":
                self.has_utf8only = True
                log.info("Server is UTF8ONLY")
            elif key == "BOT":
                self.bot_mode_char = value if value else "B"
                log.info("Server supports bot mode (mode char: %s)", self.bot_mode_char)
                if self._registered:
                    await self._send(f"MODE {self.nick} +{self.bot_mode_char}")

    async def _handle_cap(self, params: list[str]) -> None:
        if len(params) < 3:
            return

        subcommand = params[1]
        cap_list = params[-1]

        # CAP LS 302 can be multi-line: a "*" before the cap list means
        # more lines follow.  Collect all caps first, send one REQ.
        if subcommand == "LS":
            # Check for multi-line continuation ("*" before the trailing param)
            is_multiline = len(params) >= 4 and params[2] == "*"

            for cap in cap_list.split():
                cap_name = cap.split("=")[0]
                if cap_name == "draft/relaymsg":
                    self.has_relaymsg = True
                    if "=" in cap:
                        self.relaymsg_separator = cap.split("=", 1)[1][0]
                    self._pending_caps.append(cap_name)
                    log.info(
                        "RELAYMSG supported (separator=%r)",
                        self.relaymsg_separator,
                    )
                elif cap_name == "message-tags":
                    self.has_message_tags = True
                    self._pending_caps.append(cap_name)
                    log.info("message-tags supported")
                elif cap_name in ("draft/message-ids", "message-ids"):
                    self._pending_caps.append(cap_name)
                    log.info("message-ids supported")
                elif cap_name == "echo-message":
                    self.has_echo_message = True
                    self._pending_caps.append(cap_name)
                    log.info("echo-message supported")
                elif cap_name == "batch":
                    self.has_batch = True
                    self._pending_caps.append(cap_name)
                    log.info("batch supported")
                elif cap_name in ("draft/multiline", "multiline"):
                    self.has_multiline = True
                    if "=" in cap:
                        for kv in cap.split("=", 1)[1].split(","):
                            if "=" not in kv:
                                continue
                            k, v = kv.split("=", 1)
                            try:
                                if k == "max-bytes":
                                    self.multiline_max_bytes = int(v)
                                elif k == "max-lines":
                                    self.multiline_max_lines = int(v)
                            except ValueError:
                                pass
                    self._pending_caps.append(cap_name)
                    log.info(
                        "multiline supported (max-bytes=%d, max-lines=%d)",
                        self.multiline_max_bytes, self.multiline_max_lines,
                    )
                elif cap_name == "away-notify":
                    self._pending_caps.append(cap_name)
                    log.info("away-notify supported")
                elif cap_name == "sasl" and self.sasl_password:
                    self._has_sasl = True
                    self._pending_caps.append(cap_name)
                    log.info("SASL supported, will authenticate")

            if is_multiline:
                return  # Wait for the final LS line

            # Final LS line - send a single REQ for all collected caps
            if self._pending_caps:
                await self._send(
                    f"CAP REQ :{' '.join(self._pending_caps)}"
                )
                self._pending_caps.clear()
            else:
                await self._send("CAP END")

        elif subcommand == "ACK":
            if (
                self._has_sasl
                and self.sasl_password
                and not self._sasl_started
            ):
                self._sasl_started = True
                await self._send("AUTHENTICATE PLAIN")
            else:
                await self._send("CAP END")

        elif subcommand == "NAK":
            log.warning("Server rejected capabilities: %s", cap_list)
            await self._send("CAP END")

    async def _handle_authenticate(self, params: list[str]) -> None:
        if not params or params[0] != "+":
            return
        # SASL PLAIN: base64(\0nick\0password)
        payload = f"\x00{self.nick}\x00{self.sasl_password}"
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        await self._send(f"AUTHENTICATE {encoded}")

    async def _handle_mode(self, params: list[str]) -> None:
        if len(params) < 3:
            return
        channel = params[0].lower()
        modes = params[1]
        mode_args = params[2:]

        if channel not in self.channels:
            return

        adding = True
        arg_idx = 0
        for ch in modes:
            if ch == "+":
                adding = True
            elif ch == "-":
                adding = False
            elif ch == "o":
                if arg_idx < len(mode_args):
                    target = mode_args[arg_idx]
                    arg_idx += 1
                    if target.lower() == self.nick.lower():
                        self.channels[channel] = adding
                        log.info(
                            "%s in %s: op=%s",
                            self.nick, channel, adding,
                        )
            elif ch in "beIkflqaohvns":
                if ch in "beIkohvqa":
                    arg_idx += 1

    async def _handle_privmsg(
        self, tags: dict[str, str], prefix: str, params: list[str]
    ) -> None:
        if len(params) < 2:
            return
        target = params[0]
        text = params[1]
        nick = prefix.split("!")[0] if "!" in prefix else prefix

        # If this PRIVMSG is part of an active multiline batch, accumulate
        # it and defer dispatch until the batch closes.
        batch_ref = tags.get("batch")
        if batch_ref and batch_ref in self._inbound_batches:
            batch = self._inbound_batches[batch_ref]
            if batch.batch_type in ("draft/multiline", "multiline"):
                if not batch.nick:
                    batch.nick = nick
                concat = "draft/multiline-concat" in tags or "multiline-concat" in tags
                batch.lines.append((text, concat))
                return

        if not target.startswith(("#", "&")):
            return

        is_self = nick == self.nick
        if not is_self and self.has_relaymsg and self.relaymsg_separator in nick:
            suffix = nick.split(self.relaymsg_separator)[-1]
            if suffix == "xmpp":
                is_self = True

        if is_self:
            # echo-message: capture msgid for reply threading before dropping
            echo_msgid = tags.get("msgid")
            if echo_msgid and self.on_self_message:
                await self.on_self_message(target, echo_msgid)
            return

        msgid = tags.get("msgid") or None
        reply_to_msgid = tags.get("+reply") or None

        # CTCP ACTION (/me)
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            action_text = text[8:-1]
            if self.on_action:
                irc_msg = IRCMessage(
                    channel=target, nick=nick, text=action_text,
                    is_action=True, msgid=msgid,
                )
                await self.on_action(irc_msg)
            return

        if self.on_message:
            irc_msg = IRCMessage(
                channel=target, nick=nick, text=text,
                msgid=msgid, reply_to_msgid=reply_to_msgid,
            )
            await self.on_message(irc_msg)

    async def _handle_batch(
        self, tags: dict[str, str], prefix: str, params: list[str]
    ) -> None:
        if not params:
            return
        ref_token = params[0]
        if not ref_token or ref_token[0] not in "+-":
            return
        sign, ref = ref_token[0], ref_token[1:]

        if sign == "+":
            if len(params) < 2:
                return
            batch_type = params[1]
            target = params[2] if len(params) >= 3 else ""
            self._inbound_batches[ref] = _InboundBatch(
                batch_type=batch_type,
                target=target,
                msgid=tags.get("msgid") or None,
                reply_to_msgid=tags.get("+reply") or None,
            )
            return

        # sign == "-": close the batch and dispatch combined message
        batch = self._inbound_batches.pop(ref, None)
        if batch is None:
            return
        if batch.batch_type not in ("draft/multiline", "multiline"):
            return
        if not batch.lines or not batch.target.startswith(("#", "&")):
            return

        # Re-assemble lines: \n separator unless line carries the concat tag
        parts: list[str] = []
        for i, (line_text, concat) in enumerate(batch.lines):
            if i > 0 and not concat:
                parts.append("\n")
            parts.append(line_text)
        text = "".join(parts)

        nick = batch.nick
        if not nick:
            return

        is_self = nick == self.nick
        if not is_self and self.has_relaymsg and self.relaymsg_separator in nick:
            suffix = nick.split(self.relaymsg_separator)[-1]
            if suffix == "xmpp":
                is_self = True
        if is_self:
            if batch.msgid and self.on_self_message:
                await self.on_self_message(batch.target, batch.msgid)
            return

        if self.on_message:
            irc_msg = IRCMessage(
                channel=batch.target, nick=nick, text=text,
                msgid=batch.msgid, reply_to_msgid=batch.reply_to_msgid,
            )
            await self.on_message(irc_msg)

    async def _handle_tagmsg(
        self, tags: dict[str, str], prefix: str, params: list[str]
    ) -> None:
        if not params:
            return
        target = params[0]
        nick = prefix.split("!")[0] if "!" in prefix else prefix

        if not target.startswith(("#", "&")):
            return
        if nick == self.nick:
            return

        typing_value = tags.get("+typing", tags.get("typing", ""))
        if typing_value and self.on_typing:
            is_typing = typing_value == "active"
            await self.on_typing(target, nick, is_typing)

        react_value = tags.get("+draft/react", "")
        unreact_value = tags.get("+draft/unreact", "")
        if (react_value or unreact_value) and self.on_reaction:
            emoji = react_value or unreact_value
            reply_to = tags.get("+reply") or None
            await self.on_reaction(target, nick, emoji, reply_to, bool(unreact_value))
