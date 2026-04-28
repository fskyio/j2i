# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Native IRCv3 multiline support for relaying multi-line content
- IRC-to-XMPP text formatting conversion

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
