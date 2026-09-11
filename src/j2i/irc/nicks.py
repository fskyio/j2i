from __future__ import annotations

import hashlib
import re

# Characters legal in IRC nicks (broadly permissive, covers most ircds)
_IRC_NICK_ILLEGAL = re.compile(r"[^a-zA-Z0-9_\-\[\]\\`^{}|()]")

DEFAULT_NICK_LEN = 30
_HASH_LEN = 7
_FALLBACK_SEP = "|"


def sanitize_irc_nick(nick: str) -> str:
    sanitized = _IRC_NICK_ILLEGAL.sub("-", nick)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    sanitized = sanitized.strip("-")
    return sanitized or "unknown"


def casemap_nick(nick: str, mapping: str = "ascii") -> str:
    """Case-fold an IRC nick for equality checks.

    ``rfc1459``/``strict-rfc1459`` map ``{}|^`` onto ``[]\\~``. Anything else
    (including ``ascii`` and ``rfc7613``) uses Unicode/ASCII lowercasing.
    """
    lowered = nick.lower()
    if mapping in ("rfc1459", "strict-rfc1459"):
        return (
            lowered.replace("{", "[")
            .replace("}", "]")
            .replace("|", "\\")
            .replace("^", "~")
        )
    return lowered


def prepare_base_nick(nick: str) -> str:
    """Sanitize and ensure the nick does not start with a digit or hyphen."""
    base = sanitize_irc_nick(nick)
    if base and base[0] in "0123456789-":
        base = "_" + base
    return base


def preferred_puppet_nick(
    xmpp_nick: str,
    suffix: str,
    separator: str,
    nicklen: int,
) -> str:
    """Build the preferred connected-puppet nick, truncated to ``nicklen``."""
    base = prepare_base_nick(xmpp_nick)
    nicklen = max(1, nicklen)
    if not suffix:
        return base[:nicklen]
    extra = len(separator) + len(suffix)
    if extra >= nicklen:
        room_for_suffix = nicklen - 1 - len(separator)
        if room_for_suffix < 0:
            return base[:nicklen]
        suffix = suffix[:room_for_suffix]
        extra = len(separator) + len(suffix)
    room = nicklen - extra
    return base[: max(1, room)] + separator + suffix


def collision_puppet_nick(
    xmpp_nick: str,
    identity: str,
    nicklen: int,
    separator: str = _FALLBACK_SEP,
) -> str:
    """Deterministic fallback nick: ``base<sep><7 hex chars of sha256(identity)``."""
    base = prepare_base_nick(xmpp_nick)
    nicklen = max(1, nicklen)
    sep = separator if len(separator) == 1 else _FALLBACK_SEP
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:_HASH_LEN]
    extra = len(sep) + _HASH_LEN
    if extra >= nicklen:
        return digest[:nicklen]
    return base[: nicklen - extra] + sep + digest


def puppet_identity(irc_name: str, xmpp_nick: str) -> str:
    return f"{irc_name}\0{xmpp_nick}"


def allocate_puppet_nick(
    xmpp_nick: str,
    *,
    suffix: str,
    separator: str,
    nicklen: int,
    taken: set[str],
    identity: str,
    mapping: str = "ascii",
) -> str | None:
    """Pick a nick that is not in ``taken`` (already casemapped).

    Tries the preferred form, then the hash fallback. Returns None if both
    collide with ``taken``.
    """
    preferred = preferred_puppet_nick(xmpp_nick, suffix, separator, nicklen)
    if casemap_nick(preferred, mapping) not in taken:
        return preferred
    fallback = collision_puppet_nick(xmpp_nick, identity, nicklen, separator)
    if casemap_nick(fallback, mapping) not in taken:
        return fallback
    return None
