# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Split over-long relayed messages into multiple IRC lines on UTF-8/word boundaries so the server no longer silently truncates them; the split pieces count toward `max_lines`, so oversized pastes still fall back to the pastebin
- Auto-detect the server's line-length limit from the IRCv3 `LINELEN` ISUPPORT token, with a new `max_line_bytes` setting (global or per-bridge) to override the ceiling
- Test suite (pytest) covering the pure translation helpers: nick sanitizing, IRC→XMPP formatting, pastebin service resolution, and reply/reaction prefix building

### Fixed
- Redact credentials (SASL payload, NickServ `IDENTIFY` password, server `PASS`) from debug logs; the command remains visible but the secret is masked as `<redacted>`

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
