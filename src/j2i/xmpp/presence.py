from __future__ import annotations

from dataclasses import dataclass

_NS_MUC_USER = "http://jabber.org/protocol/muc#user"
_AWAY_SHOWS = frozenset({"away", "xa", "dnd"})


@dataclass(frozen=True)
class OccupantEvent:
    """A real MUC occupant's presence, already classified."""

    muc_jid: str
    nick: str
    kind: str  # join | leave | nick
    is_self: bool = False
    new_nick: str | None = None
    # None = available / back; str (possibly empty) = away-like show
    away_reason: str | None = None
    status_codes: frozenset[str] = frozenset()
    reason: str | None = None
    actor: str | None = None


def muc_user_info(
    xml,
) -> tuple[set[str], str | None, str | None, str | None]:
    """Parse muc#user status codes, ban reason, actor, and item nick."""
    muc_x = xml.find(f"{{{_NS_MUC_USER}}}x")
    if muc_x is None:
        return set(), None, None, None
    codes = {
        s.get("code")
        for s in muc_x.findall(f"{{{_NS_MUC_USER}}}status")
        if s.get("code")
    }
    item = muc_x.find(f"{{{_NS_MUC_USER}}}item")
    reason = None
    actor = None
    item_nick = None
    if item is not None:
        item_nick = item.get("nick") or None
        reason_el = item.find(f"{{{_NS_MUC_USER}}}reason")
        if reason_el is not None and reason_el.text:
            reason = reason_el.text.strip() or None
        actor_el = item.find(f"{{{_NS_MUC_USER}}}actor")
        if actor_el is not None:
            actor = actor_el.get("nick") or actor_el.get("jid") or None
    return codes, reason, actor, item_nick


def parse_occupant_event(
    *,
    muc_jid: str,
    nick: str,
    ptype: str,
    self_nick: str,
    codes: set[str],
    item_nick: str | None = None,
    show: str = "",
    status: str = "",
    reason: str | None = None,
    actor: str | None = None,
) -> OccupantEvent | None:
    """Classify a MUC presence as join, leave, or nick-change.

    Error/subscribe/probe stanzas return None. Code 110 or ``nick == self_nick``
    marks ``is_self``; the caller still decides whether to act.
    """
    if not nick:
        return None
    is_self = "110" in codes or nick == self_nick
    code_set = frozenset(codes)

    if ptype == "unavailable":
        if "303" in codes and item_nick:
            return OccupantEvent(
                muc_jid=muc_jid,
                nick=nick,
                kind="nick",
                is_self=is_self,
                new_nick=item_nick,
                status_codes=code_set,
            )
        return OccupantEvent(
            muc_jid=muc_jid,
            nick=nick,
            kind="leave",
            is_self=is_self,
            status_codes=code_set,
            reason=reason,
            actor=actor,
        )

    if ptype in ("", "available"):
        away_reason = None
        if show in _AWAY_SHOWS:
            away_reason = status or show
        return OccupantEvent(
            muc_jid=muc_jid,
            nick=nick,
            kind="join",
            is_self=is_self,
            away_reason=away_reason,
            status_codes=code_set,
        )

    return None


def occupant_event_from_presence(pres, self_nick: str) -> OccupantEvent | None:
    """Build an OccupantEvent from a slixmpp Presence stanza."""
    nick = pres["from"].resource
    if not nick:
        return None
    codes, reason, actor, item_nick = muc_user_info(pres.xml)
    return parse_occupant_event(
        muc_jid=str(pres["from"].bare),
        nick=nick,
        ptype=pres["type"],
        self_nick=self_nick,
        codes=codes,
        item_nick=item_nick,
        show=pres["show"] or "",
        status=pres["status"] or "",
        reason=reason,
        actor=actor,
    )
