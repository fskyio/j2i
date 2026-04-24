from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from j2i.config import load_config
from j2i.bridge import Bridge


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="j2i",
        description="XMPP-to-IRC bridge",
    )
    parser.add_argument(
        "-c", "--config",
        default="config.toml",
        help="Path to config file (default: config.toml)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    bridge = Bridge(config)
    asyncio.run(_run(bridge))


async def _run(bridge: Bridge) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        if not stop_event.is_set():
            bridge.stop()
            stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _on_signal)
    loop.add_signal_handler(signal.SIGTERM, _on_signal)

    await bridge.start()
    await stop_event.wait()


if __name__ == "__main__":
    main()
