"""Connection manager tests.

Covers the state machine, handshake, version gate, capability parsing,
heartbeat, reconnection and generation fencing.
"""

from __future__ import annotations

import threading

import pytest

from companion import constants
from companion.manager import CompanionConnectionManager, ConnectionStatus
from companion.models import CompanionConnectionSettings, ConnectionState

from .fake_satellite import FakeSatelliteServer, find_closed_port, _wait_until

GOOD_BEGIN = b'BEGIN CompanionVersion="4.3.4" ApiVersion="1.10.1"\n'
GOOD_CAPS = b"CAPS SUBSCRIPTIONS=1\n"
FULL_GREETING = GOOD_BEGIN + GOOD_CAPS


def _settings(port: int, host: str = "127.0.0.1") -> CompanionConnectionSettings:
    return CompanionConnectionSettings(
        host=host, port=port, device_id="streamcontroller-companion-test"
    )


class StatusLog:
    """Records every status transition the manager publishes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.states: list[ConnectionState] = []

    def __call__(self, status: ConnectionStatus) -> None:
        with self.lock:
            self.states.append(status.state)

    def saw(self, state: ConnectionState) -> bool:
        with self.lock:
            return state in self.states


@pytest.fixture
def manager_factory():
    created: list[CompanionConnectionManager] = []

    def make(settings: CompanionConnectionSettings) -> CompanionConnectionManager:
        manager = CompanionConnectionManager(settings)
        created.append(manager)
        return manager

    yield make

    for manager in created:
        manager.stop()


def _wait_state(manager, state: ConnectionState, timeout: float = 8.0) -> bool:
    return _wait_until(lambda: manager.status.state is state, timeout)


# --- Handshake -------------------------------------------------------------


class TestHandshake:
    def test_reaches_connected_after_begin_and_caps(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.CONNECTED)
            status = manager.status
            assert status.api_version == "1.10.1"
            assert status.companion_version == "4.3.4"
            assert status.subscriptions_supported is True

    def test_publishes_the_full_state_sequence(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            log = StatusLog()
            manager.add_status_listener(log)
            manager.start()

            assert _wait_state(manager, ConnectionState.CONNECTED)
            assert log.saw(ConnectionState.CONNECTING)
            assert log.saw(ConnectionState.NEGOTIATING)
            assert log.saw(ConnectionState.CONNECTED)

    def test_pings_immediately_on_connect(self, manager_factory):
        """Companion drops a connection silent for ~5s, so the first ping
        must not wait a full heartbeat interval."""
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.CONNECTED)
            assert server.wait_for_line("PING", timeout=1.0)

    def test_connects_without_caps_after_the_timeout(self, manager_factory):
        """A Companion that never sends CAPS must not leave us stuck."""
        with FakeSatelliteServer(greeting=GOOD_BEGIN) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.CONNECTED)
            assert manager.status.subscriptions_supported is False

    def test_parses_bitmap_formats_when_offered(self, manager_factory):
        greeting = GOOD_BEGIN + b"CAPS SUBSCRIPTIONS=1 BITMAP_FORMATS=png,webp\n"
        with FakeSatelliteServer(greeting=greeting) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.CONNECTED)
            assert manager.status.capabilities.negotiated_bitmap_format == "png"

    def test_absent_bitmap_formats_means_raw(self, manager_factory):
        """The live Companion's behaviour: SUBSCRIPTIONS only, so raw rgb."""
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.CONNECTED)
            assert manager.status.capabilities.uses_raw_bitmaps is True

    def test_reports_missing_subscription_support(self, manager_factory):
        with FakeSatelliteServer(greeting=GOOD_BEGIN + b"CAPS SUBSCRIPTIONS=0\n") as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.CONNECTED)
            assert manager.status.subscriptions_supported is False


# --- Version gate ----------------------------------------------------------


class TestVersionGate:
    def test_old_api_version_is_rejected(self, manager_factory):
        greeting = b'BEGIN CompanionVersion="3.0.0" ApiVersion="1.9.0"\n'
        with FakeSatelliteServer(greeting=greeting) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.INCOMPATIBLE)
            assert constants.MINIMUM_SATELLITE_API_VERSION in manager.status.last_error

    def test_missing_api_version_is_rejected(self, manager_factory):
        with FakeSatelliteServer(greeting=b'BEGIN CompanionVersion="3.0.0"\n') as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.INCOMPATIBLE)

    def test_exact_minimum_version_is_accepted(self, manager_factory):
        greeting = (
            f'BEGIN ApiVersion="{constants.MINIMUM_SATELLITE_API_VERSION}"\n'.encode()
            + GOOD_CAPS
        )
        with FakeSatelliteServer(greeting=greeting) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.CONNECTED)

    def test_incompatible_does_not_cause_a_reconnect_storm(self, manager_factory):
        """This is not a transient socket error."""
        greeting = b'BEGIN ApiVersion="1.0.0"\n'
        with FakeSatelliteServer(greeting=greeting) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()

            assert _wait_state(manager, ConnectionState.INCOMPATIBLE)
            connections_after_reject = server.connection_count

            # Well past the shortest backoff delay.
            assert not _wait_until(
                lambda: server.connection_count > connections_after_reject + 1, 4.0
            ), "manager kept reconnecting to an incompatible Companion"


# --- Reconnection ----------------------------------------------------------


class TestReconnection:
    def test_connects_when_companion_appears_later(self, manager_factory):
        """Companion not running at StreamController startup."""
        port = find_closed_port()
        manager = manager_factory(_settings(port))
        manager.start()

        assert _wait_state(manager, ConnectionState.DISCONNECTED, timeout=5.0)

        server = FakeSatelliteServer(greeting=FULL_GREETING)
        server._listener = None
        try:
            import socket

            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(4)
            listener.settimeout(0.2)
            server._listener = listener
            server.port = port
            server._stop.clear()
            server._thread = threading.Thread(target=server._serve, daemon=True)
            server._thread.start()

            assert _wait_state(manager, ConnectionState.CONNECTED, timeout=20.0)
        finally:
            server.stop()

    def test_recovers_after_companion_drops_the_connection(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            server.drop_client()

            assert _wait_state(manager, ConnectionState.DISCONNECTED, timeout=5.0)
            assert _wait_state(manager, ConnectionState.CONNECTED, timeout=20.0)
            assert server.connection_count >= 2

    def test_recovers_after_an_abrupt_reset(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            server.reset_client()

            assert _wait_state(manager, ConnectionState.CONNECTED, timeout=20.0)

    def test_generation_advances_on_every_attempt(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)
            first = manager.generation

            server.drop_client()
            # Wait for the loss to register before waiting for recovery,
            # otherwise the still-CONNECTED state satisfies the wait instantly.
            assert _wait_state(manager, ConnectionState.DISCONNECTED, timeout=5.0)
            assert _wait_state(manager, ConnectionState.CONNECTED, timeout=20.0)

            assert manager.generation > first

    def test_backoff_resets_after_a_successful_connection(self, manager_factory):
        """A healthy endpoint must not inherit delay from earlier failures.

        Without the reset, successive drops walk the backoff up to its maximum
        and stay there, so a blip on a working Companion takes ten seconds to
        recover instead of one.
        """
        import time

        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            recoveries = []
            for _ in range(3):
                generation = manager.generation
                server.drop_client()

                started = time.monotonic()
                assert _wait_until(
                    lambda g=generation: (
                        manager.status.state is ConnectionState.CONNECTED
                        and manager.generation > g
                    ),
                    20.0,
                )
                recoveries.append(time.monotonic() - started)

            # Accumulating backoff would give roughly 1s, 2s, 4s. With the reset
            # every recovery stays near the first delay.
            assert max(recoveries) < 4.0, (
                f"recovery times grew, suggesting backoff did not reset: {recoveries}"
            )

    def test_retry_now_skips_the_backoff(self, manager_factory):
        port = find_closed_port()
        manager = manager_factory(_settings(port))
        manager.start()
        assert _wait_state(manager, ConnectionState.DISCONNECTED, timeout=5.0)

        before = manager.generation
        manager.retry_now()

        assert _wait_until(lambda: manager.generation > before, 5.0)


# --- Settings changes ------------------------------------------------------


class TestSettingsChanges:
    def test_changing_host_reconnects_to_the_new_endpoint(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as first, FakeSatelliteServer(
            greeting=FULL_GREETING
        ) as second:
            manager = manager_factory(_settings(first.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            manager.update_settings(_settings(second.port))

            assert _wait_until(lambda: second.connection_count >= 1, 20.0)
            assert _wait_state(manager, ConnectionState.CONNECTED, timeout=20.0)
            assert manager.status.endpoint == f"127.0.0.1:{second.port}"

    def test_identical_settings_do_not_reconnect(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            settings = _settings(server.port)
            manager = manager_factory(settings)
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)
            generation = manager.generation

            manager.update_settings(_settings(server.port))

            assert not _wait_until(lambda: manager.generation != generation, 2.0)


# --- Messages and listeners ------------------------------------------------


class TestMessageFanOut:
    def test_forwards_unconsumed_messages_with_their_generation(self, manager_factory):
        received: list[tuple[str, int]] = []
        lock = threading.Lock()

        def record(msg, generation) -> None:
            with lock:
                received.append((msg.command, generation))

        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.add_message_listener(record)
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            # An unrecognised command is what reaches listeners; everything the
            # manager understands, including BRIGHTNESS, is consumed.
            server.send_line("SOME-FUTURE-COMMAND FOO=bar")

            assert _wait_until(
                lambda: any(cmd == "SOME-FUTURE-COMMAND" for cmd, _ in received), 5.0
            )
            command, generation = next(
                c for c in received if c[0] == "SOME-FUTURE-COMMAND"
            )
            assert generation == manager.generation

    def test_handshake_messages_are_consumed_not_forwarded(self, manager_factory):
        forwarded: list[str] = []

        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.add_message_listener(lambda msg, gen: forwarded.append(msg.command))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            assert "BEGIN" not in forwarded
            assert "CAPS" not in forwarded
            assert "PONG" not in forwarded

    def test_imagery_messages_are_consumed_not_forwarded(self, manager_factory):
        """KEY-STATE, SUB-STATE and KEYS-CLEAR are routed to the registry."""
        forwarded: list[str] = []

        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.add_message_listener(lambda msg, gen: forwarded.append(msg.command))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            server.send_line("KEY-STATE KEY=0 BITMAP=aGk=")
            server.send_line("KEYS-CLEAR")
            server.send_line("BRIGHTNESS DEVICEID=x VALUE=100")
            server.send_line("KEY-PRESS OK=1 DEVICEID=x")
            server.send_line("SOME-FUTURE-COMMAND X=1")

            assert _wait_until(lambda: "SOME-FUTURE-COMMAND" in forwarded, 5.0)
            for consumed in ("KEY-STATE", "KEYS-CLEAR", "BRIGHTNESS", "KEY-PRESS"):
                assert consumed not in forwarded, f"{consumed} should be consumed"

    def test_a_raising_listener_does_not_break_the_manager(self, manager_factory):
        seen: list[str] = []

        def explode(msg, gen):
            seen.append(msg.command)
            raise RuntimeError("listener failure")

        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.add_message_listener(explode)
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            server.send_line("SOME-FUTURE-COMMAND FOO=bar")

            assert _wait_until(lambda: "SOME-FUTURE-COMMAND" in seen, 5.0)
            assert manager.status.state is ConnectionState.CONNECTED

    def test_responds_to_an_inbound_ping(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING, auto_pong=False) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)
            server.clear_received()

            server.send_line("PING")

            assert server.wait_for_line("PONG", timeout=5.0)


# --- Page navigation -------------------------------------------------------


class TestPageNavigation:
    def test_holding_navigation_registers_the_surface(self, manager_factory):
        """The hold is what makes a page-only layout able to page at all."""
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            manager.hold_page_navigation(object())

            assert server.wait_for_line("ADD-DEVICE", timeout=5.0)
            assert "CAN_CHANGE_PAGE" in server.received_text()

    def test_change_page_reaches_companion(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)
            manager.hold_page_navigation(object())
            assert server.wait_for_line("ADD-DEVICE", timeout=5.0)
            server.clear_received()

            assert manager.change_page(True) is True

            assert server.wait_for_line("CHANGE-PAGE", timeout=5.0)
            assert "DIRECTION=1" in server.received_text()

    def test_change_page_without_a_surface_is_not_sent(self, manager_factory):
        """Companion errors on an unknown DEVICEID; stay quiet instead."""
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)
            server.clear_received()

            assert manager.change_page(True) is False
            assert "CHANGE-PAGE" not in server.received_text()

    def test_a_rejected_change_page_does_not_break_the_connection(
        self, manager_factory
    ):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            server.send_line(
                'CHANGE-PAGE ERROR=1 DEVICEID="dev" MESSAGE="Missing DIRECTION"'
            )

            assert _wait_until(
                lambda: manager.status.state is ConnectionState.CONNECTED, 2.0
            )


# --- Robustness ------------------------------------------------------------


class TestRobustness:
    def test_malformed_traffic_does_not_break_the_connection(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            server.send(b"\x00\x01\x02 garbage without newline")
            server.send_line("!!! not a real command")
            server.send_line("KEY-STATE")  # missing required params
            server.send(bytes(range(256)))
            server.send_line("KEYS-CLEAR")

            assert manager.status.state is ConnectionState.CONNECTED
            assert _wait_until(lambda: server.has_client, 2.0)

    def test_stop_is_idempotent(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            for _ in range(3):
                manager.stop()

            assert manager.status.state is ConnectionState.DISCONNECTED

    def test_stop_prevents_further_reconnection(self, manager_factory):
        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            manager = manager_factory(_settings(server.port))
            manager.start()
            assert _wait_state(manager, ConnectionState.CONNECTED)

            manager.stop()
            count = server.connection_count

            assert not _wait_until(lambda: server.connection_count > count, 4.0)

    def test_send_is_refused_when_not_connected(self, manager_factory):
        manager = manager_factory(_settings(find_closed_port()))
        from companion import protocol

        assert manager.send(protocol.ping()) is False

    def test_no_thread_leak_across_start_stop_cycles(self, manager_factory):
        baseline = threading.active_count()

        with FakeSatelliteServer(greeting=FULL_GREETING) as server:
            for _ in range(5):
                manager = CompanionConnectionManager(_settings(server.port))
                manager.start()
                _wait_state(manager, ConnectionState.CONNECTED, timeout=5.0)
                manager.stop()

        assert _wait_until(lambda: threading.active_count() <= baseline + 1, 8.0), (
            f"threads grew from {baseline} to {threading.active_count()}"
        )
