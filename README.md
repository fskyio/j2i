# j2i

A bridge between XMPP MUCs and IRC channels. Supports both basic plumbing (bot relays messages as text) and puppeteering mode (messages appear from the actual sender's nick).

## Features

- **Basic plumbing mode** - bridge bot relays messages in `<nick> text` format, works with any XMPP and IRC server
- **Puppeteering** - XMPP users appear on IRC with their real nick via [RELAYMSG](https://raw.githubusercontent.com/ircv3/ircv3-specifications/66233655658dce029fc2a5184a0ab97201a4ceec/extensions/relaymsg.md) or opt-in nick puppets (extra IRC connections); IRC users appear in XMPP MUCs as puppet JIDs via [XEP-0114 component](https://xmpp.org/extensions/xep-0114.html)
- **Ban syncing** - optional: when a component puppet is banned in a MUC, the corresponding IRC nick is given `+b` and kicked. Off by default (`sync_bans`)
- **Smart replies** - XEP-0461 replies from XMPP become `nick: ` mentions or quoted on IRC; IRCv3 reply tags are preserved
- **Reactions** - XMPP reactions (XEP-0444) are relayed to IRC as attributed text; IRC `+draft/react`/`+draft/unreact` tags are bridged natively to XMPP reactions
- **Message edits** - XEP-0308 corrections are relayed to IRC as `* corrected text`
- **Pastebin** - messages exceeding a configurable line limit are uploaded to a pastebin and linked instead of flooding
- **Typing indicators** - XEP-0085 (XMPP) ↔ IRCv3 typing tag
- **Multiline messages** - IRCv3 [draft/multiline](https://ircv3.net/specs/extensions/multiline) batches are joined into a single XMPP message; multi-line XMPP messages are sent to IRC as one batch when supported, with per-line fallback otherwise
- **Anti-ping** - zero-width space inserted into relayed nicks to avoid unwanted highlights
- **Multiple networks** - bridge as many XMPP/IRC connections and channel pairs as you want, each configured independently

## Requirements

- Python 3.11+
- slixmpp

## Installation

### pip/pipx (PyPI)

You can install j2i from PyPI with pip:

```sh
pip install j2i
```

Or with pipx for an isolated environment:

```sh
pipx install j2i
```

### pip/pipx (FSKY Foundry)

To download the package from FSKY Foundry instead of PyPI:

```sh
pip install j2i --pip-args="--index-url https://foundry.fsky.io/api/packages/fsky/pypi/simple --extra-index-url https://pypi.org/simple"
```

Or with pipx:

```sh
pipx install j2i --pip-args="--index-url https://foundry.fsky.io/api/packages/fsky/pypi/simple --extra-index-url https://pypi.org/simple"
```

### From wheel

Download the wheel from the [releases page](https://foundry.fsky.io/fsky/j2i/releases) and install with pip:

```sh
pip install j2i-*.whl
```

## Running

### Installed package

After installing, simply run:

```sh
j2i -c config.toml
```

### Local development

Requires [uv](https://docs.astral.sh/uv/):

```sh
git clone https://foundry.fsky.io/fsky/j2i.git
cd j2i
cp config.example.toml config.toml
$EDITOR config.toml
uv run j2i -c config.toml
```

### Docker/Podman

The image expects the config file at `/config/config.toml`.

```sh
docker run -v ./config.toml:/config/config.toml foundry.fsky.io/fsky/j2i:latest
```

### Podman quadlet (systemd)

A quadlet unit file is provided in `contrib/quadlet/j2i.container`. It runs the container as a systemd user service with auto-update enabled and a read-only filesystem.

To install, place the unit file into `.config/containers/systemd/` or `/etc/containers/systemd/` and run:

```sh
systemctl --user daemon-reload
systemctl --user start j2i.service
```

## Configuration

Copy `config.example.toml` and edit it. The example file has comments explaining every option.

The config has four sections:

- `[[xmpp]]` - one entry per XMPP account or component; set `component = true` for XEP-0114 component mode
- `[[irc]]` - one entry per IRC network; set `relaymsg = true` to enable RELAYMSG, or `puppet_mode = "nicks"` for connected nick puppets
- `[[bridge]]` - one entry per MUC↔channel pair, referencing the `name` fields above
- `[settings]` - global defaults (`anti_ping`, `max_lines`, `pastebin`, etc.); can be overridden per `[[bridge]]`

### Basic plumbing mode (simple setup)

Set `component = false` in `[[xmpp]]` and `relaymsg = false` in `[[irc]]`. The bridge connects as a regular XMPP user and IRC bot and relays messages as `<nick> text`. No special server configuration needed.

### Puppeteering mode (full setup)

**IRC side (RELAYMSG):** Set `relaymsg = true` in `[[irc]]`. The IRC bot must have operator status (`+o`) in the channel. The bridge detects RELAYMSG support on connect and falls back to prefixed bot messages (`<nick> text`) if unavailable.

**IRC side (nick puppets):** Set `puppet_mode = "nicks"` (or `"auto"`) to connect a separate IRC nick per XMPP occupant. This is how you get native IRCv3 reactions and typing as that nick; RELAYMSG cannot send `TAGMSG`. Nicks look like `alice|xmpp` by default (`|` is legal in a real nick; `/` is not). Pool size and idle QUIT are per-network (`max_puppets`, `puppet_idle_seconds`; `0` means unlimited / never). Public networks often cap connections per IP — this is intended for small rooms or an ircd you control.

`puppet_mode` values: `relaymsg` (default, current behaviour), `nicks` (never RELAYMSG), `auto` (RELAYMSG then nicks), `prefix` (always `<nick> text`). Existing configs that omit these keys are unchanged.

**XMPP side:** Set `component = true` in `[[xmpp]]` and configure your XMPP server with a component subdomain. Each IRC user will appear in the MUC as a puppet JID under that domain (e.g. `johndoe.libera@irc.example.org`). RELAYMSG nicks on IRC get a `/xmpp` suffix (e.g. `alice/xmpp`) to distinguish them from real IRC users.

Optional `sync_bans` (global, or per `[[xmpp]]` / `[[irc]]` / `[[bridge]]`) forwards a live MUC ban of a puppet to IRC as `MODE +b nick!*@*` plus `KICK`. The IRC bot needs channel operator status; if it does not have it, the failure is logged and ignored. Kicks-without-ban, unbans, and IRC→XMPP bans are not synced.

## Support chatroom
If you want to ask anything or need assistance with j2i, we have a public chatroom on XMPP and IRC.

- XMPP: [j2i@room.telepath.im](xmpp:j2i@room.telepath.im?join)
- IRC: #j2i on [irc.telepath.im](https://telepath.im/irc/)

The MUC and channel are bridged together with j2i.

## License

This project is released into the public domain under the [Unlicense](LICENSE).
