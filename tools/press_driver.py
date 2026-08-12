#!/usr/bin/env python3
"""Drive presses on a Companion button from a separate Satellite connection.

Companion pushes the resulting feedback change to *every* subscribed surface,
so this exercises the running StreamController plugin's render path without a
physical finger on the deck. Used to measure render latency.

    python3 tools/press_driver.py --host 192.168.50.245 --row 3 --column 1 --presses 20
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion import constants, protocol  # noqa: E402
from companion.manager import CompanionConnectionManager  # noqa: E402
from companion.models import (  # noqa: E402
    CompanionAddress,
    CompanionConnectionSettings,
    ConnectionState,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=constants.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=constants.DEFAULT_PORT)
    parser.add_argument("--row", type=int, default=3, help="0-based row")
    parser.add_argument("--column", type=int, default=1, help="0-based column")
    parser.add_argument("--presses", type=int, default=20)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--hold", type=float, default=0.15)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    manager = CompanionConnectionManager(
        CompanionConnectionSettings(
            host=args.host,
            port=args.port,
            device_id="streamcontroller-companion-driver",
        )
    )
    address = CompanionAddress.from_ui(
        dynamic_page=True, row=args.row, column=args.column
    )

    manager.start()
    deadline = time.monotonic() + 20
    while (
        time.monotonic() < deadline
        and manager.status.state is not ConnectionState.CONNECTED
    ):
        time.sleep(0.05)

    if manager.status.state is not ConnectionState.CONNECTED:
        print("could not connect")
        return 1

    # Register the surface so the address is addressable.
    manager.attach(address, lambda a, i: None)
    time.sleep(2.0)

    print(f"pressing {address.describe()} {args.presses}x "
          f"(hold {args.hold}s, every {args.interval}s)")
    for index in range(args.presses):
        manager.key_down(address)
        time.sleep(args.hold)
        manager.key_up(address)
        print(f"  press {index + 1}/{args.presses}", flush=True)
        time.sleep(max(0.0, args.interval - args.hold))

    manager.surface.unregister()
    manager.stop()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
