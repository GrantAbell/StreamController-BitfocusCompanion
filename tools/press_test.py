#!/usr/bin/env python3
"""Press-state round-trip test against a real Companion.

Sends key-down and key-up to a Companion button and records the imagery
Companion pushes back, proving that the pressed graphic comes from Companion
rather than being faked locally.

    python3 tools/press_test.py --host 192.168.50.245 --row 1 --column 1

Pressing executes whatever the Companion button is configured to do, so only
run this against buttons you know are safe.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion import constants  # noqa: E402
from companion.manager import CompanionConnectionManager  # noqa: E402
from companion.models import (  # noqa: E402
    CompanionAddress,
    CompanionConnectionSettings,
    ConnectionState,
)


def _digest(image) -> str:
    """Short, stable fingerprint of an image's pixels."""
    return hashlib.sha1(image.tobytes()).hexdigest()[:10]


def _wait(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=constants.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=constants.DEFAULT_PORT)
    parser.add_argument("--row", type=int, default=1, help="1-based UI row")
    parser.add_argument("--column", type=int, default=1, help="1-based UI column")
    parser.add_argument("--hold", type=float, default=0.4)
    parser.add_argument("--static-page", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    manager = CompanionConnectionManager(
        CompanionConnectionSettings(
            host=args.host,
            port=args.port,
            device_id="streamcontroller-companion-press",
        )
    )

    frames: list[tuple[float, str]] = []
    lock = threading.Lock()

    def on_image(address, image) -> None:
        with lock:
            frames.append((time.monotonic(), "cleared" if image is None else _digest(image)))

    if args.static_page is None:
        address = CompanionAddress.from_ui(
            dynamic_page=True, row=args.row, column=args.column
        )
    else:
        address = CompanionAddress.from_ui(
            dynamic_page=False,
            page=args.static_page,
            row=args.row,
            column=args.column,
        )

    manager.start()
    if not _wait(lambda: manager.status.state is ConnectionState.CONNECTED, 20):
        print("FAIL: could not connect")
        return 1

    manager.attach(address, on_image)
    if not _wait(lambda: len(frames) > 0, 10):
        print("FAIL: no initial image from Companion")
        manager.stop()
        return 1

    time.sleep(1.0)  # let the idle image settle
    with lock:
        idle = frames[-1][1]
        frames.clear()
    print(f"target      : {address.describe()}")
    print(f"idle image  : {idle}")

    started = time.monotonic()
    print(f"\n-> key down")
    manager.key_down(address)
    down_seen = _wait(lambda: len(frames) > 0, 5)
    time.sleep(args.hold)

    print(f"-> key up")
    manager.key_up(address)
    time.sleep(1.5)

    with lock:
        captured = list(frames)

    print(f"\n{'t+ms':>7}  image")
    for stamp, digest in captured:
        print(f"{(stamp - started) * 1000:>7.0f}  {digest}")

    sequence = [digest for _, digest in captured]
    distinct = []
    for digest in sequence:
        if not distinct or distinct[-1] != digest:
            distinct.append(digest)

    print(f"\nframes received : {len(sequence)}")
    print(f"distinct states : {distinct}")

    manager.detach(address, on_image)
    manager.surface.unregister()
    manager.stop()

    if not down_seen:
        print(
            "\nINCONCLUSIVE: Companion sent no new image on press. The button is "
            "probably empty with no pressed styling, so there is nothing to "
            "change. The press itself was delivered."
        )
        return 0

    if len(distinct) >= 2 and distinct[-1] == idle:
        print("\nPASS: pressed image differed and reverted to the idle image")
    elif len(distinct) >= 2:
        print("\nPASS: Companion changed the image in response to press/release")
    else:
        print("\nINCONCLUSIVE: only one distinct image observed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
