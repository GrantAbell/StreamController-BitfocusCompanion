#!/usr/bin/env python3
"""Reconnection soak test against a real Companion.

Runs a local TCP proxy in front of Companion and kills the proxied connection
repeatedly. From the plugin's point of view this is indistinguishable from
Companion vanishing, so it exercises the restoration sequence
against real Companion behaviour without touching the Companion install.

    python3 tools/reconnect_soak.py --host 192.168.50.245 --cycles 10
"""

from __future__ import annotations

import argparse
import logging
import socket
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

log = logging.getLogger("soak")


class KillableProxy:
    """Forwards TCP to Companion and can sever the link on demand."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.3)
        self.port = self._listener.getsockname()[1]

        self._stop = threading.Event()
        self._live: list[socket.socket] = []
        self._lock = threading.Lock()
        self.connections = 0

        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                upstream = socket.create_connection(self.target, timeout=5)
            except OSError as exc:
                log.warning("proxy could not reach Companion: %s", exc)
                client.close()
                continue

            with self._lock:
                self._live.extend((client, upstream))
                self.connections += 1

            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.close()
                except OSError:
                    pass

    def sever(self) -> None:
        """Drop every proxied connection, as if Companion had gone away."""
        with self._lock:
            live, self._live = self._live, []
        for sock in live:
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.sever()
        try:
            self._listener.close()
        except OSError:
            pass


def _wait(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=constants.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=constants.DEFAULT_PORT)
    parser.add_argument("--cycles", type=int, default=10)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    proxy = KillableProxy(args.host, args.port)
    manager = CompanionConnectionManager(
        CompanionConnectionSettings(
            host="127.0.0.1",
            port=proxy.port,
            device_id="streamcontroller-companion-soak",
        )
    )

    images: list[int] = []
    lock = threading.Lock()

    def on_image(address, image) -> None:
        with lock:
            images.append(0 if image is None else 1)

    dynamic = CompanionAddress.from_ui(dynamic_page=True, row=1, column=1)
    static = CompanionAddress.from_ui(dynamic_page=False, page=1, row=1, column=1)

    manager.start()
    if not _wait(lambda: manager.status.state is ConnectionState.CONNECTED, 20):
        print("FAIL: never connected")
        return 1

    manager.attach(dynamic, on_image)
    manager.attach(static, on_image)
    time.sleep(2)

    print(f"connected via proxy; Companion api {manager.status.api_version}")
    print(f"{'cycle':>5}  {'recovered':>9}  {'secs':>5}  {'surface':>8}  {'subs':>4}  {'images':>6}")

    failures = 0
    for cycle in range(1, args.cycles + 1):
        with lock:
            images.clear()

        generation_before = manager.generation
        proxy.sever()

        started = time.monotonic()
        ok = _wait(
            lambda: manager.status.state is ConnectionState.CONNECTED
            and manager.generation > generation_before,
            30,
        )
        # Give Companion a moment to push imagery for the rebuilt surface.
        got_images = _wait(lambda: len(images) > 0, 10)
        elapsed = time.monotonic() - started

        geometry = manager.surface.registered_geometry
        subs = len(manager.subscriptions.active_addresses)
        with lock:
            count = len(images)

        healthy = ok and got_images and geometry is not None and subs == 2
        if not healthy:
            failures += 1

        print(
            f"{cycle:>5}  {'yes' if healthy else 'NO':>9}  {elapsed:>5.1f}  "
            f"{geometry.describe() if geometry else '-':>8}  {subs:>4}  {count:>6}"
        )

    manager.surface.unregister()
    manager.stop()
    proxy.close()

    print(f"\nproxy accepted {proxy.connections} connections")
    if failures:
        print(f"FAIL: {failures}/{args.cycles} cycles did not fully recover")
        return 1
    print(f"PASS: {args.cycles}/{args.cycles} cycles recovered with surface and subscriptions restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
