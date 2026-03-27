from __future__ import annotations

import argparse
import asyncio
import logging
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
    try:
        asyncio.run(_run(bridge))
    except KeyboardInterrupt:
        pass


async def _run(bridge: Bridge) -> None:
    await bridge.start()
    # Keep running until cancelled
    await asyncio.Event().wait()


if __name__ == "__main__":
    main()
