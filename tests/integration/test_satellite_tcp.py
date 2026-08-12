"""Satellite TCP transport tests against a fake Companion endpoint."""

from __future__ import annotations

import threading
import time

import pytest

from companion import protocol
from companion.transports.base import TransportCallbacks
from companion.transports.satellite_tcp import SatelliteTcpTransport

from .fake_satellite import FakeSatelliteServer, find_closed_port, _wait_until


class Recorder:
    """Collects transport callbacks so tests can assert on them."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.close_reasons: list[str | None] = []
        self.chunks = bytearray()
        self.connect_count = 0

    def callbacks(self) -> TransportCallbacks:
        return TransportCallbacks(
            on_connected=self._on_connected,
            on_data=self._on_data,
            on_closed=self._on_closed,
        )

    def _on_connected(self) -> None:
        with self.lock:
            self.connect_count += 1
        self.connected.set()

    def _on_data(self, chunk: bytes) -> None:
        with self.lock:
            self.chunks.extend(chunk)

    def _on_closed(self, reason: str | None) -> None:
        with self.lock:
            self.close_reasons.append(reason)
        self.closed.set()

    @property
    def data(self) -> str:
        with self.lock:
            return self.chunks.decode("utf-8", errors="replace")

    def wait_for_data(self, needle: str, timeout: float = 3.0) -> bool:
        return _wait_until(lambda: needle in self.data, timeout)


@pytest.fixture
def server():
    with FakeSatelliteServer() as fake:
        yield fake


@pytest.fixture
def recorder():
    return Recorder()


def _transport(host: str, port: int, recorder: Recorder, **kwargs):
    return SatelliteTcpTransport(host, port, recorder.callbacks(), **kwargs)


def _connect_both_sides(transport, recorder: Recorder, server: FakeSatelliteServer):
    """Connect and wait until *both* ends agree a client exists.

    The transport's on_connected fires as soon as its socket completes, which
    can beat the server's accept loop to registering the client. Sending from
    the server before that point would silently no-op, so every test that
    drives the server after connecting must synchronise on both sides.
    """
    transport.connect()
    assert recorder.connected.wait(3.0), "transport never reported connected"
    assert server.wait_for_client(3.0), "server never registered the client"


# --- Connecting ------------------------------------------------------------


class TestConnecting:
    def test_connects_and_reports_it(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            transport.connect()

            assert recorder.connected.wait(3.0)
            assert transport.is_connected
            assert server.wait_for_client()
        finally:
            transport.disconnect()

    def test_refused_connection_reports_a_reason_without_raising(self, recorder):
        """Companion not running at startup must be a normal, described outcome."""
        transport = _transport("127.0.0.1", find_closed_port(), recorder)
        try:
            transport.connect()

            assert recorder.closed.wait(5.0)
            assert not transport.is_connected
            assert recorder.close_reasons[0], "a refusal must carry a description"
        finally:
            transport.disconnect()

    def test_unresolvable_host_reports_a_reason(self, recorder):
        transport = _transport(
            "no-such-host.invalid", 16622, recorder, connect_timeout=2.0
        )
        try:
            transport.connect()

            assert recorder.closed.wait(8.0)
            assert recorder.close_reasons[0]
        finally:
            transport.disconnect()

    def test_description_is_log_friendly(self, recorder):
        assert _transport("192.168.50.245", 16622, recorder).description == (
            "tcp://192.168.50.245:16622"
        )

    def test_double_connect_starts_one_worker(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            transport.connect()
            transport.connect()

            assert recorder.connected.wait(3.0)
            time.sleep(0.3)
            assert server.connection_count == 1
        finally:
            transport.disconnect()


# --- Receiving -------------------------------------------------------------


class TestReceiving:
    def test_receives_a_greeting(self, recorder):
        greeting = b'BEGIN ApiVersion="1.10.1"\nCAPS SUBSCRIPTIONS=1\n'
        with FakeSatelliteServer(greeting=greeting) as server:
            transport = _transport("127.0.0.1", server.port, recorder)
            try:
                transport.connect()

                assert recorder.wait_for_data("CAPS SUBSCRIPTIONS=1")
                assert "BEGIN" in recorder.data
            finally:
                transport.disconnect()

    def test_receives_a_payload_larger_than_one_read(self, server, recorder):
        """A 20736-char bitmap arrives across several recv() calls."""
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)

            payload = "A" * 20736
            server.send_line(f"KEY-STATE KEY=0 BITMAP={payload}")

            assert recorder.wait_for_data(payload, timeout=5.0)
        finally:
            transport.disconnect()

    def test_many_rapid_messages_all_arrive(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)

            for index in range(200):
                server.send_line(f"KEY-STATE KEY={index} BITMAP=x")

            assert recorder.wait_for_data("KEY=199", timeout=5.0)
            assert recorder.data.count("KEY-STATE") == 200
        finally:
            transport.disconnect()


# --- Sending ---------------------------------------------------------------


class TestSending:
    def test_sends_a_message(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)

            assert transport.send(protocol.ping()) is True
            assert server.wait_for_line("PING")
        finally:
            transport.disconnect()

    def test_send_before_connected_is_refused_not_queued(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            assert transport.send(protocol.ping()) is False
        finally:
            transport.disconnect()

    def test_send_after_disconnect_is_refused(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        transport.connect()
        assert recorder.connected.wait(3.0)
        transport.disconnect()

        assert transport.send(protocol.ping()) is False

    def test_send_never_blocks_the_caller(self, server, recorder):
        """Input handlers call send() directly, so it must return at once."""
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)

            start = time.monotonic()
            for _ in range(500):
                transport.send(protocol.ping())
            elapsed = time.monotonic() - start

            assert elapsed < 0.5, f"send() took {elapsed:.3f}s for 500 messages"
        finally:
            transport.disconnect()

    def test_rapid_sends_arrive_in_order(self, server, recorder):
        """Rapid dial rotation must not reorder or drop events."""
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)

            for index in range(100):
                transport.send(protocol.key_press("dev", index, pressed=True))

            assert server.wait_for_line("KEY=99", timeout=5.0)
            lines = [line for line in server.received_lines() if "KEY-PRESS" in line]
            assert len(lines) == 100
            assert [int(line.split("KEY=")[1].split(" ")[0]) for line in lines] == list(
                range(100)
            )
        finally:
            transport.disconnect()


# --- Losing the connection -------------------------------------------------


class TestConnectionLoss:
    def test_graceful_close_is_reported(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)

            server.drop_client()

            assert recorder.closed.wait(5.0)
            assert not transport.is_connected
        finally:
            transport.disconnect()

    def test_abrupt_reset_is_reported_not_raised(self, server, recorder):
        """A crashed Companion must surface as a close, never an exception."""
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)
            transport.send(protocol.ping())

            server.reset_client()

            assert recorder.closed.wait(5.0)
            assert not transport.is_connected
        finally:
            transport.disconnect()

    def test_server_disappearing_entirely_is_reported(self, recorder):
        server = FakeSatelliteServer()
        server.start()
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)

            server.stop()

            assert recorder.closed.wait(5.0)
        finally:
            transport.disconnect()

    def test_closed_is_reported_only_once(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)
            server.drop_client()
            assert recorder.closed.wait(5.0)
            time.sleep(0.4)

            assert len(recorder.close_reasons) == 1
        finally:
            transport.disconnect()


# --- Shutdown --------------------------------------------------------------


class TestShutdown:
    def test_disconnect_is_idempotent(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        _connect_both_sides(transport, recorder, server)

        for _ in range(5):
            transport.disconnect()

        assert not transport.is_connected

    def test_disconnect_without_connect_is_safe(self, recorder):
        _transport("127.0.0.1", 16622, recorder).disconnect()

    def test_disconnect_suppresses_the_close_callback(self, server, recorder):
        """A deliberate shutdown is not a connection failure."""
        transport = _transport("127.0.0.1", server.port, recorder)
        _connect_both_sides(transport, recorder, server)

        transport.disconnect()
        time.sleep(0.4)

        assert recorder.close_reasons == []

    def test_disconnect_interrupts_a_blocked_read_promptly(self, server, recorder):
        """The socketpair wakeup is what makes this fast."""
        transport = _transport("127.0.0.1", server.port, recorder)
        _connect_both_sides(transport, recorder, server)

        start = time.monotonic()
        transport.disconnect()
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"disconnect took {elapsed:.3f}s"

    def test_no_thread_leak_across_many_cycles(self, server, recorder):
        baseline = threading.active_count()

        for _ in range(20):
            transport = _transport("127.0.0.1", server.port, recorder)
            transport.connect()
            recorder.connected.wait(3.0)
            transport.disconnect()

        assert _wait_until(lambda: threading.active_count() <= baseline + 1, 5.0), (
            f"threads grew from {baseline} to {threading.active_count()}"
        )

    def test_reconnect_after_disconnect_works(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)
            transport.disconnect()

            recorder.connected.clear()
            transport.connect()

            assert recorder.connected.wait(3.0)
            assert transport.is_connected
            assert server.wait_for_connection_count(2)
        finally:
            transport.disconnect()


# --- Robustness ------------------------------------------------------------


class TestRobustness:
    def test_a_raising_callback_does_not_kill_the_worker(self, server):
        """Listener errors stay contained."""
        received = threading.Event()
        calls = []

        def exploding_on_data(chunk: bytes) -> None:
            calls.append(chunk)
            received.set()
            raise RuntimeError("listener blew up")

        callbacks = TransportCallbacks(
            on_connected=lambda: None,
            on_data=exploding_on_data,
            on_closed=lambda reason: None,
        )
        transport = SatelliteTcpTransport("127.0.0.1", server.port, callbacks)
        try:
            transport.connect()
            assert _wait_until(lambda: transport.is_connected, 3.0)

            server.send_line("PING")
            assert received.wait(3.0)

            # The worker must still be alive and processing.
            received.clear()
            server.send_line("PONG")
            assert received.wait(3.0)
            assert len(calls) >= 2
        finally:
            transport.disconnect()

    def test_binary_garbage_does_not_crash_the_transport(self, server, recorder):
        transport = _transport("127.0.0.1", server.port, recorder)
        try:
            _connect_both_sides(transport, recorder, server)

            server.send(bytes(range(256)) * 8)
            server.send_line("PING")

            assert recorder.wait_for_data("PING", timeout=5.0)
            assert transport.is_connected
        finally:
            transport.disconnect()
