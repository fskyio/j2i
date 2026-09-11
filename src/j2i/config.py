from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# How XMPP occupants are attributed on IRC (not whether rooms are bridged).
PUPPET_MODES = frozenset({"relaymsg", "nicks", "auto", "prefix"})

# Characters legal in an IRC nick; the puppet separator must be one of these.
_NICK_CHAR = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-[]\\`^{}|()")


@dataclass
class XMPPConfig:
    name: str
    password: str
    jid: str = ""
    component: bool = False
    component_domain: str | None = None
    component_host: str = "localhost"
    component_port: int = 5347
    nick: str = "IRC Bridge"
    # Bot avatar: a local file path or http(s) URL. None = use the bundled
    # default; "" (empty string) = explicitly no avatar.
    avatar: str | None = None
    # Sync XMPP puppet bans to IRC (+b and kick). None = defer to global.
    sync_bans: bool | None = None


@dataclass
class IRCConfig:
    name: str
    host: str
    nick: str
    port: int = 6697
    tls: bool = True
    sasl_password: str | None = None
    nickserv_password: str | None = None
    relaymsg: bool = True
    relaymsg_suffix: str | None = None
    # Override the line-length ceiling (bytes) for this network. None/0 = defer
    # to the global setting, else auto-detect from ISUPPORT LINELEN.
    max_line_bytes: int | None = None
    # Sync XMPP puppet bans to IRC (+b and kick). None = defer to XMPP/global.
    sync_bans: bool | None = None
    # IRC nick-puppet overrides (None = use the matching [settings] irc_* default)
    puppet_mode: str | None = None
    max_puppets: int | None = None
    puppet_idle_seconds: int | None = None
    # None = inherit suffix resolution; "" = unsuffixed nicks
    puppet_suffix: str | None = None
    puppet_separator: str | None = None


@dataclass
class BridgeMapping:
    xmpp: str
    xmpp_muc: str
    irc: str
    irc_channel: str
    # Per-bridge overrides (None = use global settings)
    anti_ping: bool | None = None
    max_lines: int | None = None
    max_line_bytes: int | None = None
    pastebin: str | None = None
    pastebin_auth: str | None = None
    pastebin_field: str | None = None
    # Reply style: "quote" (inline excerpt) or "ping" (nick mention only)
    reply_style: str | None = None
    # Sync XMPP puppet bans to IRC (+b and kick). None = defer to irc/xmpp/global.
    sync_bans: bool | None = None


@dataclass
class Settings:
    anti_ping: bool = True
    max_lines: int = 20
    # Max IRC protocol line length (bytes) to assume when splitting long
    # messages. 0 = auto-detect from the server's ISUPPORT LINELEN (or 512).
    # Set this to override/raise the ceiling on servers that allow more.
    max_line_bytes: int = 0
    pastebin: str | None = None
    pastebin_auth: str | None = None
    pastebin_field: str | None = None
    # Reply style: "quote" (inline excerpt) or "ping" (nick mention only)
    reply_style: str = "quote"
    # Suffix appended to spoofed nicks in RELAYMSG (e.g. spoofednick/xmpp)
    relaymsg_suffix: str = "xmpp"
    # How XMPP occupants are attributed on IRC. "relaymsg" preserves today's
    # send path (default). "nicks" uses extra TCP connections. "auto" tries
    # RELAYMSG then nicks. "prefix" always sends <nick> text from the bot.
    irc_puppet_mode: str = "relaymsg"
    # Extra IRC connections for nick puppets. 0 = unlimited. Unused unless
    # puppet_mode is "nicks" or "auto".
    irc_max_puppets: int = 16
    # QUIT idle puppets after this many seconds. 0 = never.
    irc_puppet_idle_seconds: int = 600
    # Suffix for connected puppet nicks (e.g. alice|xmpp). None = inherit
    # relaymsg_suffix. "" = unsuffixed nicks.
    irc_puppet_suffix: str | None = None
    # Single legal nick character between base and suffix. "/" is not legal.
    irc_puppet_separator: str = "|"
    # Avatar byte budget: the re-encoded image must fit under this so the
    # base64 vcard-temp stanza stays under the server's max stanza size.
    # This is the real, server-enforced limit (image dimensions are cosmetic).
    avatar_byte_cap: int = 64 * 1024
    # Longest-side pixel target avatars are downscaled to before encoding.
    # XEP-0153 suggests small, square images; this is a display nicety.
    avatar_target_px: int = 96
    # Hide the package version from XEP-0092, the XMPP resource, and disco identity.
    hide_version: bool = False
    # Omit OS from XEP-0092 replies (resource/disco never include it).
    hide_os: bool = False
    # When a component puppet is banned in a MUC (status 301), set +b on the
    # corresponding IRC nick and kick them. Off by default; override per
    # [[xmpp]], [[irc]], or [[bridge]] (most specific wins).
    sync_bans: bool = False


@dataclass
class Config:
    xmpp: list[XMPPConfig] = field(default_factory=list)
    irc: list[IRCConfig] = field(default_factory=list)
    bridges: list[BridgeMapping] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    def xmpp_by_name(self, name: str) -> XMPPConfig:
        for x in self.xmpp:
            if x.name == name:
                return x
        raise KeyError(f"No XMPP config named {name!r}")

    def irc_by_name(self, name: str) -> IRCConfig:
        for i in self.irc:
            if i.name == name:
                return i
        raise KeyError(f"No IRC config named {name!r}")


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)

    xmpp_configs = [XMPPConfig(**entry) for entry in raw.get("xmpp", [])]
    irc_configs = [IRCConfig(**entry) for entry in raw.get("irc", [])]
    bridges = [BridgeMapping(**entry) for entry in raw.get("bridge", [])]
    settings = Settings(**raw.get("settings", {}))

    cfg = Config(
        xmpp=xmpp_configs,
        irc=irc_configs,
        bridges=bridges,
        settings=settings,
    )

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    xmpp_names = {x.name for x in cfg.xmpp}
    irc_names = {i.name for i in cfg.irc}

    if len(xmpp_names) != len(cfg.xmpp):
        raise ValueError("Duplicate XMPP config names")
    if len(irc_names) != len(cfg.irc):
        raise ValueError("Duplicate IRC config names")

    for b in cfg.bridges:
        if b.xmpp not in xmpp_names:
            raise ValueError(
                f"Bridge references unknown XMPP config {b.xmpp!r}"
            )
        if b.irc not in irc_names:
            raise ValueError(
                f"Bridge references unknown IRC config {b.irc!r}"
            )

    for x in cfg.xmpp:
        if x.component and not x.component_domain:
            raise ValueError(
                f"XMPP config {x.name!r} has component=true but no component_domain"
            )
        if not x.component and not x.jid:
            raise ValueError(
                f"XMPP config {x.name!r} requires a jid when component=false"
            )

    _validate_puppet_mode("settings.irc_puppet_mode", cfg.settings.irc_puppet_mode)
    _validate_puppet_separator(
        "settings.irc_puppet_separator", cfg.settings.irc_puppet_separator
    )
    _validate_non_negative("settings.irc_max_puppets", cfg.settings.irc_max_puppets)
    _validate_non_negative(
        "settings.irc_puppet_idle_seconds", cfg.settings.irc_puppet_idle_seconds
    )
    if cfg.settings.irc_puppet_suffix:
        _validate_nick_fragment(
            "settings.irc_puppet_suffix", cfg.settings.irc_puppet_suffix
        )

    for i in cfg.irc:
        if i.puppet_mode is not None:
            _validate_puppet_mode(f"irc {i.name!r} puppet_mode", i.puppet_mode)
        if i.puppet_separator is not None:
            _validate_puppet_separator(
                f"irc {i.name!r} puppet_separator", i.puppet_separator
            )
        if i.max_puppets is not None:
            _validate_non_negative(f"irc {i.name!r} max_puppets", i.max_puppets)
        if i.puppet_idle_seconds is not None:
            _validate_non_negative(
                f"irc {i.name!r} puppet_idle_seconds", i.puppet_idle_seconds
            )
        if i.puppet_suffix:
            _validate_nick_fragment(f"irc {i.name!r} puppet_suffix", i.puppet_suffix)


def _validate_puppet_mode(label: str, mode: str) -> None:
    if mode not in PUPPET_MODES:
        allowed = ", ".join(sorted(PUPPET_MODES))
        raise ValueError(f"{label} must be one of: {allowed} (got {mode!r})")


def _validate_puppet_separator(label: str, sep: str) -> None:
    if len(sep) != 1 or sep not in _NICK_CHAR:
        raise ValueError(
            f"{label} must be a single legal IRC nick character "
            f"(got {sep!r})"
        )


def _validate_nick_fragment(label: str, value: str) -> None:
    if any(ch not in _NICK_CHAR for ch in value):
        raise ValueError(f"{label} contains characters illegal in an IRC nick")


def _validate_non_negative(label: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{label} must be >= 0 (got {value})")


def resolve_puppet_mode(irc: IRCConfig, settings: Settings) -> str:
    if irc.puppet_mode is not None:
        return irc.puppet_mode
    return settings.irc_puppet_mode


def resolve_max_puppets(irc: IRCConfig, settings: Settings) -> int:
    if irc.max_puppets is not None:
        return irc.max_puppets
    return settings.irc_max_puppets


def resolve_puppet_idle_seconds(irc: IRCConfig, settings: Settings) -> int:
    if irc.puppet_idle_seconds is not None:
        return irc.puppet_idle_seconds
    return settings.irc_puppet_idle_seconds


def resolve_puppet_separator(irc: IRCConfig, settings: Settings) -> str:
    if irc.puppet_separator is not None:
        return irc.puppet_separator
    return settings.irc_puppet_separator


def resolve_puppet_suffix(irc: IRCConfig, settings: Settings) -> str:
    """Empty string means unsuffixed nicks; None at every layer inherits RELAYMSG."""
    if irc.puppet_suffix is not None:
        return irc.puppet_suffix
    if settings.irc_puppet_suffix is not None:
        return settings.irc_puppet_suffix
    if irc.relaymsg_suffix is not None:
        return irc.relaymsg_suffix
    return settings.relaymsg_suffix
