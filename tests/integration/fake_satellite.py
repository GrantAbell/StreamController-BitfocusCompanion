"""A scriptable stand-in for Companion's Satellite TCP endpoint.

Lets the connection tests run without a real Companion:
server absent, server appearing later, server vanishing mid-session, abrupt
resets, unsupported versions and missing capabilities.
"""

from __future__ import annotations

import socket
import threading
import time


class FakeSatelliteServer:
    """A minimal TCP server that speaks just enough Satellite to drive tests."""

    def __init__(
        self,
        *,
        greeting: bytes | None = None,
        auto_pong: bool = True,
    ) -> None:
        self.greeting = greeting
        self.auto_pong = auto_pong

        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._lock = threading.Lock()
        self._client: socket.socket | None = None
        self._received = bytearray()

        self.connection_count = 0
        self.port = 0

    # --- Lifecycle ---------------------------------------------------------

    def start(self) -> int:
        """Listen on an ephemeral port and return it."""
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]

        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        """Stop listening and drop any client. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.drop_client()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    def __enter__(self) -> FakeSatelliteServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # --- Server loop -------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, _addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            with self._lock:
                self._client = client
                self.connection_count += 1

            if self.greeting:
                try:
                    client.sendall(self.greeting)
                except OSError:
                    pass

            self._read_client(client)

    def _read_client(self, client: socket.socket) -> None:
        client.settimeout(0.2)
        while not self._stop.is_set():
            try:
                chunk = client.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break

            with self._lock:
                if self._client is not client:
                    break  # superseded by a newer connection
                self._received.extend(chunk)

            if self.auto_pong and b"PING" in chunk:
                try:
                    client.sendall(b"PONG\n")
                except OSError:
                    break

        with self._lock:
            if self._client is client:
                self._client = None
        try:
            client.close()
        except OSError:
            pass

    # --- Test controls -----------------------------------------------------

    def send(self, payload: bytes) -> bool:
        """Push raw bytes to the connected client."""
        with self._lock:
            client = self._client
        if client is None:
            return False
        try:
            client.sendall(payload)
            return True
        except OSError:
            return False

    def send_line(self, line: str) -> bool:
        return self.send(line.encode() + b"\n")

    def drop_client(self) -> None:
        """Close the client connection the way a graceful shutdown would."""
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    def reset_client(self) -> None:
        """Force an RST, simulating a crash or a yanked cable."""
        with self._lock:
            client, self._client = self._client, None
        if client is None:
            return
        try:
            client.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, _linger_immediately()
            )
            client.close()
        except OSError:
            pass

    # --- Assertions --------------------------------------------------------

    @property
    def has_client(self) -> bool:
        with self._lock:
            return self._client is not None

    def received_text(self) -> str:
        with self._lock:
            return self._received.decode("utf-8", errors="replace")

    def received_lines(self) -> list[str]:
        return [line for line in self.received_text().split("\n") if line]

    def clear_received(self) -> None:
        with self._lock:
            self._received.clear()

    def wait_for_line(self, needle: str, timeout: float = 3.0) -> bool:
        """Block until a line containing ``needle`` has been received."""
        return _wait_until(lambda: needle in self.received_text(), timeout)

    def wait_for_client(self, timeout: float = 3.0) -> bool:
        return _wait_until(lambda: self.has_client, timeout)

    def wait_for_connection_count(self, count: int, timeout: float = 5.0) -> bool:
        return _wait_until(lambda: self.connection_count >= count, timeout)


def _linger_immediately() -> bytes:
    import struct

    return struct.pack("ii", 1, 0)


def _wait_until(predicate, timeout: float, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def find_closed_port() -> int:
    """Return a port that is bound and released, so connecting will be refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
