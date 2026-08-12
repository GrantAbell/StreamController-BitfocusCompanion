#!/usr/bin/env python3
"""Connect to a real Companion and report what it says.

A development aid for exercising the transport, parser and serializer without
StreamController in the way. Run from the plugin root:

    python3 tools/probe_companion.py --host 192.168.50.245

It registers a throwaway surface, watches the imagery Companion pushes, and
removes the surface again on exit.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion import constants, protocol  # noqa: E402
from companion.models import CompanionCapabilities  # noqa: E402
from companion.protocol import LineFramer, parse_stream  # noqa: E402
from companion.transports.base import TransportCallbacks  # noqa: E402
from companion.transports.satellite_tcp import SatelliteTcpTransport  # noqa: E402

log = logging.getLogger("probe")


class Probe:
    def __init__(self, host: str, port: int, device_id: str, register: bool) -> None:
        self.device_id = device_id
        self.register = register
        self.framer = LineFramer()
        self.caps: CompanionCapabilities | None = None
        self.finished = threading.Event()
        self.key_images: dict[int, int] = {}
        self.commands_seen: dict[str, int] = {}
        self.registered = False

        self.transport = SatelliteTcpTransport(
            host,
            port,
            TransportCallbacks(
                on_connected=self.on_connected,
                on_data=self.on_data,
                on_closed=self.on_closed,
            ),
        )

    # --- Callbacks ---------------------------------------------------------

    def on_connected(self) -> None:
        print(f"connected to {self.transport.description}")

    def on_closed(self, reason: str | None) -> None:
        print(f"closed: {reason or 'clean shutdown'}")
        self.finished.set()

    def on_data(self, chunk: bytes) -> None:
        for message in parse_stream(self.framer, chunk):
            self.commands_seen[message.command] = (
                self.commands_seen.get(message.command, 0) + 1
            )
            self.handle(message)

        dropped = self.framer.take_overflow()
        if dropped:
            print(f"WARNING dropped {dropped} bytes of oversized input")

    def handle(self, message) -> None:
        command = message.command

        if command == protocol.Inbound.BEGIN:
            print(
                f"BEGIN  api={message.text('ApiVersion')} "
                f"companion={message.text('CompanionVersion')}"
            )
        elif command == protocol.Inbound.CAPS:
            self.caps = CompanionCapabilities.from_caps_params(
                {k: v for k, v in message.params.items() if isinstance(v, str)}
            )
            print(f"CAPS   {self.caps.describe()}")
            if self.register:
                self.send_add_device()
        elif command == protocol.Inbound.ADD_DEVICE:
            print(
                f"ADD-DEVICE ok={message.flag('OK')} error={message.flag('ERROR')} "
                f"{message.text('MESSAGE') or ''}"
            )
            self.registered = message.flag("OK")
        elif command == protocol.Inbound.KEY_STATE:
            index = message.integer("KEY")
            bitmap = message.text("BITMAP") or ""
            if index is not None and index not in self.key_images:
                self.key_images[index] = len(bitmap)
        elif command == protocol.Inbound.PING:
            self.transport.send(protocol.pong())
        elif command not in (protocol.Inbound.PONG,):
            print(f"other  {message.describe()[:140]}")

    # --- Actions -----------------------------------------------------------

    def send_add_device(self) -> None:
        fmt = self.caps.negotiated_bitmap_format if self.caps else None
        payload = protocol.add_device(
            self.device_id,
            rows=constants.MIN_SURFACE_ROWS,
            columns=constants.MIN_SURFACE_COLUMNS,
            bitmap_format=fmt,
        )
        print(f"  -> {payload.decode().strip()}")
        self.transport.send(payload)

    def run(self, seconds: float) -> None:
        self.transport.connect()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.finished.is_set():
            time.sleep(0.1)

        if self.registered:
            print("removing probe surface")
            self.transport.send(protocol.remove_device(self.device_id))
            time.sleep(0.5)

        self.transport.disconnect()
        self.report()

    def report(self) -> None:
        print("\n--- summary ---")
        for command, count in sorted(self.commands_seen.items()):
            print(f"  {command:<14} x{count}")
        if self.key_images:
            sizes = sorted(self.key_images.values())
            expected_raw = constants.BITMAP_SIZE * constants.BITMAP_SIZE * 3
            print(
                f"  keys with imagery: {len(self.key_images)}  "
                f"payload {sizes[0]}-{sizes[-1]} chars  "
                f"(raw {constants.BITMAP_SIZE}x{constants.BITMAP_SIZE} RGB "
                f"= {expected_raw} bytes -> {(expected_raw + 2) // 3 * 4} base64)"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=constants.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=constants.DEFAULT_PORT)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--device-id", default="streamcontroller-probe")
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="only observe the handshake; do not register a surface",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )

    probe = Probe(args.host, args.port, args.device_id, register=not args.no_register)
    probe.run(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
