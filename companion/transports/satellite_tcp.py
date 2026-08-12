"""Satellite TCP transport, built on the Python standard library.

One worker thread owns the socket for its whole life. Reads and writes are
multiplexed with :mod:`selectors`, and a socketpair acts as a wakeup channel so
that queueing a message or requesting shutdown interrupts a blocking select
immediately rather than waiting for a timeout.

No third-party dependency is used or needed to open a TCP connection.
"""

from __future__ import annotations

import errno
import logging
import queue
import selectors
import socket
import threading

from .. import constants
from .base import CompanionTransport, TransportCallbacks

log = logging.getLogger(__name__)

# Cap on unsent outbound bytes. Reached only if Companion stops reading while we
# keep queueing, at which point the connection is already broken; dropping is
# better than growing without bound.
_MAX_PENDING_WRITE_BYTES = 256 * 1024

_RECV_SIZE = 65536


class SatelliteTcpTransport(CompanionTransport):
    def __init__(
        self,
        host: str,
        port: int,
        callbacks: TransportCallbacks,
        connect_timeout: float = constants.CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        self._callbacks = callbacks
        self._connect_timeout = connect_timeout

        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

        # Set once shutdown is requested; no callback fires afterwards.
        self._stopping = threading.Event()
        self._connected = threading.Event()

        self._outbox: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._pending_bytes = 0
        self._pending_lock = threading.Lock()

        # Wakes the selector when something is queued or shutdown is requested.
        self._wake_r: socket.socket | None = None
        self._wake_w: socket.socket | None = None

        self._lifecycle_lock = threading.Lock()
        self._closed_reported = False

    # --- Public interface --------------------------------------------------

    @property
    def description(self) -> str:
        return f"tcp://{self._host}:{self._port}"

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and not self._stopping.is_set()

    def connect(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None:
                log.debug(f"Transport already running for {self.description}")
                return

            self._stopping.clear()
            self._connected.clear()
            self._closed_reported = False
            self._drain_outbox()

            self._wake_r, self._wake_w = socket.socketpair()
            self._wake_r.setblocking(False)

            self._thread = threading.Thread(
                target=self._run,
                name=f"companion-tcp-{self._host}-{self._port}",
                daemon=True,
            )
            self._thread.start()

    def disconnect(self) -> None:
        """Tear the connection down deliberately and idempotently."""
        with self._lifecycle_lock:
            thread = self._thread
            self._thread = None

        # Mark stopping first: everything else checks this before calling back.
        self._stopping.set()
        self._wake()

        if thread is not None and thread is not threading.current_thread():
            # Bounded: the worker only ever blocks in select, which the wakeup
            # interrupts. If it somehow does not exit, we still release our
            # references rather than hanging StreamController's shutdown.
            thread.join(timeout=5.0)
            if thread.is_alive():
                log.warning(f"Transport worker for {self.description} did not stop cleanly")

        self._shutdown_socket()
        self._close_wakeup()
        self._connected.clear()
        self._drain_outbox()

    def send(self, payload: bytes) -> bool:
        if self._stopping.is_set() or not self._connected.is_set():
            return False

        with self._pending_lock:
            if self._pending_bytes + len(payload) > _MAX_PENDING_WRITE_BYTES:
                log.warning(
                    f"Outbound queue full for {self.description}; dropping {len(payload)} bytes"
                )
                return False
            self._pending_bytes += len(payload)

        self._outbox.put(payload)
        self._wake()
        return True

    # --- Worker ------------------------------------------------------------

    def _run(self) -> None:
        """Own the socket for one connection attempt, then report closure."""
        reason: str | None = None
        try:
            sock = self._open_socket()
            if sock is None:
                return

            self._socket = sock
            self._connected.set()
            self._safe_callback(self._callbacks.on_connected)

            reason = self._pump(sock)
        except Exception as exc:  # noqa: BLE001 - worker must never escape
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("Transport worker failed for %s", self.description, exc_info=True)
        finally:
            self._connected.clear()
            self._shutdown_socket()
            self._report_closed(reason)

    def _open_socket(self) -> socket.socket | None:
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout
            )
        except OSError as exc:
            if not self._stopping.is_set():
                self._report_closed(_describe_os_error(exc))
            return None

        if self._stopping.is_set():
            # Shutdown raced with a successful connect.
            _quietly_close(sock)
            return None

        sock.setblocking(False)
        # Protocol messages are small and latency-sensitive; Nagle would add up
        # to 40ms to a key press for no benefit.
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock

    def _pump(self, sock: socket.socket) -> str | None:
        """Read and write until the peer goes away or we are told to stop."""
        write_buffer = bytearray()

        with selectors.DefaultSelector() as selector:
            selector.register(sock, selectors.EVENT_READ)
            if self._wake_r is not None:
                selector.register(self._wake_r, selectors.EVENT_READ)

            while not self._stopping.is_set():
                self._collect_outbound(write_buffer)

                wanted = selectors.EVENT_READ | (
                    selectors.EVENT_WRITE if write_buffer else 0
                )
                selector.modify(sock, wanted)

                for key, events in selector.select(timeout=1.0):
                    if key.fileobj is self._wake_r:
                        self._drain_wakeup()
                        continue

                    if events & selectors.EVENT_WRITE and write_buffer:
                        error = self._flush(sock, write_buffer)
                        if error is not None:
                            return error

                    if events & selectors.EVENT_READ:
                        error = self._read(sock)
                        if error is not _CONTINUE:
                            return error

        return None

    def _collect_outbound(self, write_buffer: bytearray) -> None:
        while True:
            try:
                payload = self._outbox.get_nowait()
            except queue.Empty:
                return
            write_buffer.extend(payload)

    def _flush(self, sock: socket.socket, write_buffer: bytearray) -> str | None:
        """Write what the socket will take. Returns an error string on failure."""
        try:
            sent = sock.send(write_buffer)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError as exc:
            return _describe_os_error(exc)

        if sent:
            del write_buffer[:sent]
            with self._pending_lock:
                self._pending_bytes = max(0, self._pending_bytes - sent)
        return None

    def _read(self, sock: socket.socket) -> str | None | object:
        """Read available bytes. Returns ``_CONTINUE`` to keep looping."""
        try:
            chunk = sock.recv(_RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return _CONTINUE
        except OSError as exc:
            return _describe_os_error(exc)

        if not chunk:
            return "Connection closed by Companion"

        if not self._stopping.is_set():
            self._safe_callback(self._callbacks.on_data, chunk)
        return _CONTINUE

    # --- Plumbing ----------------------------------------------------------

    def _wake(self) -> None:
        writer = self._wake_w
        if writer is None:
            return
        try:
            writer.send(b"\x00")
        except OSError:
            pass

    def _drain_wakeup(self) -> None:
        reader = self._wake_r
        if reader is None:
            return
        try:
            while reader.recv(4096):
                pass
        except (BlockingIOError, InterruptedError, OSError):
            pass

    def _close_wakeup(self) -> None:
        for sock in (self._wake_r, self._wake_w):
            if sock is not None:
                _quietly_close(sock)
        self._wake_r = None
        self._wake_w = None

    def _shutdown_socket(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            _quietly_close(sock)

    def _drain_outbox(self) -> None:
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                break
        with self._pending_lock:
            self._pending_bytes = 0

    def _report_closed(self, reason: str | None) -> None:
        """Emit on_closed exactly once, and never after disconnect()."""
        with self._lifecycle_lock:
            if self._closed_reported:
                return
            self._closed_reported = True

        if self._stopping.is_set():
            return
        self._safe_callback(self._callbacks.on_closed, reason)

    def _safe_callback(self, callback, *args) -> None:
        """Never let a listener's exception kill the worker."""
        try:
            callback(*args)
        except Exception:  # noqa: BLE001
            log.error("Transport callback raised for %s", self.description, exc_info=True)


# Sentinel meaning "no error, keep going" — distinct from None, which means a
# clean close.
_CONTINUE = object()


def _quietly_close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _describe_os_error(exc: OSError) -> str:
    """Turn a socket error into something worth putting in a log line."""
    name = errno.errorcode.get(exc.errno or 0, "")
    detail = exc.strerror or str(exc)
    return f"{detail}{f' ({name})' if name else ''}"
