# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- IRC nick puppets: opt-in extra TCP connections so XMPP occupants speak as real IRC nicks (native reactions/typing), with a per-network pool, suffix/collision scheme, and no change to existing RELAYMSG configs (`irc_puppet_mode` defaults to `relaymsg`)
- Nick-puppet occupancy: XMPP leave/kick/ban PARTs (or QUITs) the IRC nick, MUC nick changes become IRC NICK, and away/xa/dnd map to AWAY when a socket already exists
- Optional `irc_puppet_presence = "eager"` (per-network `puppet_presence`) so puppets JOIN on MUC presence; default `lazy` still JOINs on first speak, idle-QUITs lurkers, and never holds extra sockets for occupancy. Eager ignores idle timeout and will not LRU-evict a live occupant (overflow is prefixed bot text)
- Nick puppets use the original XMPP nick as IRC GECOS (ident is a sanitized form of that nick, not `j2i`); MUC nick changes `SETNAME` when the network supports it
- XMPP MUC kicks/bans of real occupants QUIT the IRC puppet with a reason (or PART that channel if the nick is still in others)

### Changed
- XMPP replies sent as a connected nick puppet omit the `(re nick: "…")` quote prefix when an IRCv3 `+reply` msgid is available
- XEP-0308 corrections still relay as `* corrected text`, and now also set `+reply` to the original IRC msgid when it is known
- IRC `/me` (CTCP ACTION) is mapped through the same reply/msgid tables as normal messages, so XMPP can native-reply and react to actions

### Fixed
- Key IRC echo-message stanza-id queues per sending connection so a puppet echo cannot be mapped to a message the master (or another puppet) just sent

## [1.2.0b1] - 2026-09-08

### Added
- XEP-0092 Software Version: the bridge answers version queries as j2i (name, package version, OS) in both client and component mode. New `[settings]` flags `hide_version` (also applies to the XMPP resource and disco identity) and `hide_os` (XEP-0092 only)
- Optional XMPP→IRC ban sync in component mode: when a puppet is banned in a MUC (status 301), the corresponding IRC nick is `+b`'d (`nick!*@*`) and kicked. Off by default; enable with `sync_bans` globally or per `[[xmpp]]` / `[[irc]]` / `[[bridge]]` (most specific wins). Missing IRC ops are logged and ignored.
- Bot avatar support via XEP-0153 (vCard-based avatars): the bridge advertises an avatar in MUCs in both client and component mode. Configurable per XMPP connection with a local path or URL (`avatar`), or a bundled default; images are decoded, downscaled, and re-encoded (Pillow) to fit a configurable byte budget so the vcard-temp stanza stays under the server's max stanza size. Lays the groundwork for bridging IRC user profile metadata to XMPP avatars
- Split over-long relayed messages into multiple IRC lines on UTF-8/word boundaries so the server no longer silently truncates them; the split pieces count toward `max_lines`, so oversized pastes still fall back to the pastebin
- Auto-detect the server's line-length limit from the IRCv3 `LINELEN` ISUPPORT token, with a new `max_line_bytes` setting (global or per-bridge) to override the ceiling
- Test suite (pytest) covering the pure translation helpers: nick sanitizing, IRC→XMPP formatting, pastebin service resolution, and reply/reaction prefix building

### Fixed
- Redact credentials (SASL payload, NickServ `IDENTIFY` password, server `PASS`) from debug logs; the command remains visible but the secret is masked as `<redacted>`
- Scope reply, reaction, and body caches by connection and room so identical IRC/XMPP identifiers from different networks cannot collide

### Changed
- Make RELAYMSG suffix configurable
- Refactored reply/reaction prefix formatting into pure helpers and de-duplicated the shared excerpt-truncation and RELAYMSG-suffix logic

## [1.1.0] - 2026-04-29

### Added
- Native IRCv3 multiline support for relaying multi-line content
- IRC-to-XMPP text formatting conversion
- IRCv3 `UTF8ONLY` ISUPPORT token detection
- IRCv3 bot mode: sets the user mode advertised by the `BOT` ISUPPORT token on registration
- Reaction bridging: IRC `+draft/react`/`+draft/unreact` tags bridged natively to XMPP; XMPP reactions bridged to IRC as attributed text messages with optional quoted context

### Changed
- XMPP resource name is now `j2i <version>` instead of the slixmpp default
- XMPP entity capabilities identity and caps node now identify the client as `j2i` rather than slixmpp
- Switched from +draft/reply to +reply for the reply tag

## [1.0.1] - 2026-04-24

### Fixed
- Corrected signal handling for `SIGINT` and `SIGTERM` for cleaner shutdown behavior

### Changed
- Renamed `Dockerfile` to `Containerfile` and adjusted container metadata labels
- Refined packaging/project metadata in `pyproject.toml`
- Updated installation instructions in the README

## [1.0.0] - 2026-04-22

### Added
- Initial stable release of `j2i`
- Puppet reconnect handling improvements for XMPP component mode
- Quote-style replies on the IRC side
