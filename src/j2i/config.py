from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    # Avatar byte budget: the re-encoded image must fit under this so the
    # base64 vcard-temp stanza stays under the server's max stanza size.
    # This is the real, server-enforced limit (image dimensions are cosmetic).
    avatar_byte_cap: int = 64 * 1024
    # Longest-side pixel target avatars are downscaled to before encoding.
    # XEP-0153 suggests small, square images; this is a display nicety.
    avatar_target_px: int = 96


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
