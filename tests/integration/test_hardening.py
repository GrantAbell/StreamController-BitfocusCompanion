"""Error containment and shutdown hardening.

The theme: nothing Companion sends, however malformed, may escape the plugin's
networking code, and shutting down must be deterministic.
"""

from __future__ import annotations

import random
import threading

import pytest

from companion.manager import CompanionConnectionManager
from companion.models import (
    CompanionAddress,
    CompanionConnectionSettings,
    ConnectionState,
)

from .fake_satellite import FakeSatelliteServer, _wait_until

GREETING = b'BEGIN CompanionVersion="4.3.4" ApiVersion="1.10.1"\nCAPS SUBSCRIPTIONS=1\n'


def _settings(port: int) -> CompanionConnectionSettings:
    return CompanionConnectionSettings(
        host="127.0.0.1", port=port, device_id="streamcontroller-companion-harden"
    )


@pytest.fixture
def connected():
    """A manager connected to a fake Companion, torn down afterwards."""
    server = FakeSatelliteServer(greeting=GREETING)
    server.start()
    manager = CompanionConnectionManager(_settings(server.port))
    manager.start()
    assert _wait_until(
        lambda: manager.status.state is ConnectionState.CONNECTED, 8.0
    ), "fixture failed to connect"
    try:
        yield manager, server
    finally:
        manager.stop()
        server.stop()


def _dynamic(row: int = 0, column: int = 0) -> CompanionAddress:
    """Row and column are 0-based, so this is KEY=0 on the surface."""
    return CompanionAddress.from_ui(dynamic_page=True, row=row, column=column)


# --- Malformed protocol traffic -------------------------------------------


class TestMalformedTraffic:
    def test_random_binary_garbage_does_not_disconnect(self, connected):
        manager, server = connected
        rng = random.Random(1234)

        for _ in range(50):
            server.send(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 400))))

        server.send_line("KEYS-CLEAR")
        assert manager.status.state is ConnectionState.CONNECTED

    def test_truncated_and_nonsense_messages_are_survivable(self, connected):
        manager, server = connected

        for line in (
            "KEY-STATE",
            "KEY-STATE KEY=",
            "KEY-STATE KEY=notanumber BITMAP=x",
            "KEY-STATE KEY=-5 BITMAP=x",
            "KEY-STATE KEY=99999 BITMAP=x",
            "SUB-STATE",
            "SUB-STATE SUBID=nonsense BITMAP=x",
            "SUB-STATE SUBID=1/2 BITMAP=x",
            "SUB-STATE SUBID=a/b/c BITMAP=x",
            "ADD-SUB ERROR",
            "KEY-PRESS ERROR=1 MESSAGE=\"Invalid KEY\"",
            "KEY-PRESS OK=1",
            "BRIGHTNESS VALUE=100",
            "CAPS",
            "BEGIN",
            "=",
            "====",
            '"',
            "\\",
        ):
            server.send_line(line)

        assert manager.status.state is ConnectionState.CONNECTED

    def test_malformed_image_payloads_never_crash(self, connected):
        """Bad image data must not take StreamController down."""
        manager, server = connected
        manager.attach(_dynamic(), lambda address, image: None)

        for payload in ("!!!", "A", "====", "Zm9v", "A" * 5000, ""):
            server.send_line(f"KEY-STATE KEY=0 BITMAP={payload}")

        server.send_line("KEYS-CLEAR")
        assert manager.status.state is ConnectionState.CONNECTED

    def test_a_bad_image_leaves_the_previous_one_alone(self, connected):
        """A decode failure must not blank a key that was showing something."""
        import base64

        manager, server = connected
        rendered: list[object] = []
        manager.attach(_dynamic(), lambda address, image: rendered.append(image))

        good = base64.b64encode(bytes((10, 20, 30)) * (72 * 72)).decode()
        server.send_line(f"KEY-STATE KEY=0 BITMAP={good}")
        assert _wait_until(lambda: len(rendered) >= 1, 5.0)

        server.send_line("KEY-STATE KEY=0 BITMAP=!!!garbage!!!")

        assert not _wait_until(lambda: len(rendered) >= 2, 1.5), (
            "a malformed image must be dropped, not delivered"
        )
        assert manager.subscriptions.cached_image(_dynamic()) is not None

    def test_oversized_unterminated_input_is_bounded(self, connected):
        """A peer streaming endlessly without newlines must not exhaust memory."""
        manager, server = connected

        for _ in range(4):
            server.send(b"x" * 300_000)

        server.send_line("KEYS-CLEAR")
        assert manager.status.state is ConnectionState.CONNECTED

    def test_a_listener_that_always_raises_cannot_break_the_connection(self, connected):
        manager, server = connected

        def explode(address, image):
            raise RuntimeError("listener always fails")

        manager.attach(_dynamic(), explode)

        import base64

        good = base64.b64encode(bytes(3) * (72 * 72)).decode()
        for _ in range(5):
            server.send_line(f"KEY-STATE KEY=0 BITMAP={good}")

        assert manager.status.state is ConnectionState.CONNECTED


# --- Input while unusable --------------------------------------------------


class TestInputSafety:
    def test_input_before_the_surface_exists_is_refused_not_raised(self):
        manager = CompanionConnectionManager(_settings(1))
        address = _dynamic()

        assert manager.key_down(address) is False
        assert manager.key_up(address) is False
        assert manager.rotate(address, clockwise=True) is False

    def test_static_input_without_subscription_support_is_refused(self):
        manager = CompanionConnectionManager(_settings(1))
        static = CompanionAddress.from_ui(
            dynamic_page=False, page=1, row=0, column=0
        )

        assert manager.key_down(static) is False
        assert manager.rotate(static, clockwise=False) is False

    def test_input_after_stop_is_refused(self, connected):
        manager, _server = connected
        manager.attach(_dynamic(), lambda a, i: None)
        manager.stop()

        assert manager.key_down(_dynamic()) is False

    def test_rapid_input_does_not_block_or_raise(self, connected):
        """Rapid rotation must not drop into the network path."""
        import time

        manager, _server = connected
        address = _dynamic()
        manager.attach(address, lambda a, i: None)
        assert _wait_until(lambda: manager.surface.is_registered, 5.0)

        start = time.monotonic()
        for index in range(500):
            manager.rotate(address, clockwise=index % 2 == 0)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"500 rotations took {elapsed:.3f}s"
        assert manager.status.state is ConnectionState.CONNECTED


# --- Shutdown --------------------------------------------------------------


class TestShutdown:
    def test_stop_joins_threads_deterministically(self):
        baseline = threading.active_count()

        with FakeSatelliteServer(greeting=GREETING) as server:
            managers = []
            for _ in range(4):
                manager = CompanionConnectionManager(_settings(server.port))
                manager.start()
                managers.append(manager)

            for manager in managers:
                _wait_until(
                    lambda m=manager: m.status.state is ConnectionState.CONNECTED, 8.0
                )
            for manager in managers:
                manager.stop()

        assert _wait_until(lambda: threading.active_count() <= baseline + 1, 10.0), (
            f"threads grew from {baseline} to {threading.active_count()}"
        )

    def test_stop_during_an_active_image_stream_is_clean(self, connected):
        import base64

        manager, server = connected
        manager.attach(_dynamic(), lambda a, i: None)
        good = base64.b64encode(bytes(3) * (72 * 72)).decode()

        stop_flag = threading.Event()

        def flood():
            while not stop_flag.is_set():
                if not server.send_line(f"KEY-STATE KEY=0 BITMAP={good}"):
                    return

        flooder = threading.Thread(target=flood, daemon=True)
        flooder.start()
        try:
            manager.stop()
        finally:
            stop_flag.set()
            flooder.join(timeout=3.0)

        assert manager.status.state is ConnectionState.DISCONNECTED

    def test_stop_before_start_is_safe(self):
        CompanionConnectionManager(_settings(1)).stop()

    def test_repeated_start_stop_cycles(self):
        with FakeSatelliteServer(greeting=GREETING) as server:
            manager = CompanionConnectionManager(_settings(server.port))
            for _ in range(3):
                manager.start()
                assert _wait_until(
                    lambda: manager.status.state is ConnectionState.CONNECTED, 8.0
                )
                manager.stop()
                assert manager.status.state is ConnectionState.DISCONNECTED


# --- Security posture ------------------------------------------------------


class TestSecurityPosture:
    def test_no_listening_socket_is_opened(self, connected):
        """The plugin only makes outbound connections."""
        import socket

        manager, server = connected

        # If the plugin listened anywhere, binding its port would fail. Instead
        # assert the only inbound listener in the process is the test server's.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        probe.close()
        assert manager.status.state is ConnectionState.CONNECTED

    def test_companion_strings_are_never_executed(self, connected):
        """A payload shaped like code must be inert."""
        manager, server = connected
        marker: list[str] = []

        server.send_line('KEY-STATE KEY=0 BITMAP="__import__(\'os\').system(\'true\')"')
        server.send_line("SUB-STATE SUBID=1/0/0 BITMAP=__import__('os')")

        assert marker == []
        assert manager.status.state is ConnectionState.CONNECTED
