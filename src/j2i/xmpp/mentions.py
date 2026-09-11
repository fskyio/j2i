from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree.ElementTree import SubElement

from j2i.irc.nicks import casemap_nick

_NS_MENTIONS = "urn:xmpp:mentions:0"

# IRC nick characters plus RELAYMSG's typical '/' separator, which is not a
# legal nick char but appears in spoofed nicks like 'alice/xmpp'.
_NICK_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "_-[]\\`^{}|()/"
)
_URL = r"https?://\S+"


@dataclass(frozen=True)
class MentionTarget:
    """How an IRC nick token should appear as an XMPP mention."""

    fallback: str
    occupant_jid: str
    occupant_id: str | None = None


@dataclass(frozen=True)
class Mention:
    """A XEP-0513 mention spanning ``[begin, end)`` in the outgoing body."""

    begin: int
    end: int
    occupant_jid: str
    occupant_id: str | None = None


def _is_token_char(char: str, extra: str) -> bool:
    return char in _NICK_CHARS or char in extra


def _url_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(_URL, text, flags=re.IGNORECASE)]


def _in_span(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)


def rewrite_irc_mentions(
    text: str,
    targets: dict[str, MentionTarget],
    mapping: str = "ascii",
    extra_chars: str = "",
) -> tuple[str, list[Mention]]:
    """Replace IRC nick tokens with XMPP fallback nicks and collect mentions.

    ``targets`` is keyed by casemapped IRC nick (the token as typed). Each
    hit is rewritten to ``fallback`` and recorded as a XEP-0513 mention
    covering that fallback in the returned body. Tokens inside ``http(s)``
    URLs are left alone.
    """
    if not text or not targets:
        return text, []

    urls = _url_spans(text)
    parts: list[str] = []
    mentions: list[Mention] = []
    out_len = 0
    i = 0
    n = len(text)
    while i < n:
        if (
            _in_span(i, urls)
            or not _is_token_char(text[i], extra_chars)
            or (i > 0 and _is_token_char(text[i - 1], extra_chars))
        ):
            parts.append(text[i])
            out_len += 1
            i += 1
            continue
        j = i + 1
        while (
            j < n
            and _is_token_char(text[j], extra_chars)
            and not _in_span(j, urls)
        ):
            j += 1
        token = text[i:j]
        target = targets.get(casemap_nick(token, mapping))
        if target is not None:
            begin = out_len
            parts.append(target.fallback)
            out_len += len(target.fallback)
            mentions.append(
                Mention(
                    begin=begin,
                    end=out_len,
                    occupant_jid=target.occupant_jid,
                    occupant_id=target.occupant_id,
                )
            )
        else:
            parts.append(token)
            out_len += len(token)
        i = j
    return "".join(parts), mentions


def attach_mentions(msg, mentions: list[Mention]) -> None:
    """Append XEP-0513 ``<mention/>`` elements to ``msg.xml``."""
    for mention in mentions:
        el = SubElement(msg.xml, f"{{{_NS_MENTIONS}}}mention")
        el.set("begin", str(mention.begin))
        el.set("end", str(mention.end))
        if mention.occupant_id:
            el.set("occupantid", mention.occupant_id)
        else:
            el.set("jid", mention.occupant_jid)


def shift_mentions(mentions: list[Mention], delta: int) -> list[Mention]:
    """Move mention offsets by ``delta`` characters (e.g. a ``/me `` prefix)."""
    if not delta or not mentions:
        return mentions
    return [
        Mention(
            begin=m.begin + delta,
            end=m.end + delta,
            occupant_jid=m.occupant_jid,
            occupant_id=m.occupant_id,
        )
        for m in mentions
    ]
