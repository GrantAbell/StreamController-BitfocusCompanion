"""The single, long-lived connection to Companion.

One manager is owned by the plugin and shared by every action.
Actions never see a socket; they register listeners and hand it addresses.

Threading:

* a **supervisor** thread owns connection lifecycle — connect, heartbeat,
  backoff, reconnect;
* the **transport** thread delivers bytes and calls back into ``_on_data``;
* **any** thread may call the public API, which never blocks on the network.

Every callback carries the connection *generation* it belongs to. A callback
from a superseded transport is discarded rather than allowed to mutate current
state, which is what stops an old reconnect worker from corrupting a new
connection.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from . import constants, protocol
from .images import decode_button_image
from .models import (
    CompanionAddress,
    CompanionCapabilities,
    CompanionConfigError,
    CompanionConnectionSettings,
    ConnectionState,
)
from .protocol import LineFramer, SatelliteMessage, parse_stream
from .subscriptions import SubscriptionRegistry
from .surface import DynamicSurface
from .transports.base import CompanionTransport, TransportCallbacks
from .transports.satellite_tcp import SatelliteTcpTransport

# Shown on static controls when the connected Companion cannot do subscriptions.
# Static buttons must fail visibly rather than silently following the active
# page.
STATIC_UNSUPPORTED = (
    "This Companion does not support static-page subscriptions"
)

log = logging.getLogger(__name__)


class _Unset:
    """Distinguishes "leave last_error alone" from "clear it to None"."""


_UNSET = _Unset()


def _compare_versions(left: str, right: str) -> int:
    """Compare dotted version strings numerically.

    Avoids a dependency on ``packaging`` for what is always a simple
    ``major.minor.patch`` comparison. A pre-release suffix such as
    ``1.11.0-beta.2`` compares on its numeric prefix, matching upstream's
    ``includePrerelease`` behaviour.
    """

    def parts(value: str) -> list[int]:
        collected: list[int] = []
        for chunk in value.split("."):
            digits = ""
            for char in chunk:
                if char.isdigit():
                    digits += char
                else:
                    break
            collected.append(int(digits) if digits else 0)
        return collected

    a, b = parts(left), parts(right)
    for index in range(max(len(a), len(b))):
        first = a[index] if index < len(a) else 0
        second = b[index] if index < len(b) else 0
        if first != second:
            return -1 if first < second else 1
    return 0


@dataclass(frozen=True)
class ConnectionStatus:
    """A snapshot of the connection, safe to hand to the UI from any thread."""

    state: ConnectionState = ConnectionState.DISCONNECTED
    endpoint: str = ""
    transport_name: str = ""
    api_version: str | None = None
    companion_version: str | None = None
    capabilities: CompanionCapabilities = field(default_factory=CompanionCapabilities)
    last_error: str | None = None
    generation: int = 0

    @property
    def subscriptions_supported(self) -> bool:
        return self.capabilities.subscriptions

    def describe(self) -> str:
        parts = [self.state.value]
        if self.endpoint:
            parts.append(self.endpoint)
        if self.api_version:
            parts.append(f"api {self.api_version}")
        if self.last_error:
            parts.append(f"error: {self.last_error}")
        return " | ".join(parts)


MessageListener = Callable[[SatelliteMessage, int], None]
StatusListener = Callable[[ConnectionStatus], None]


class CompanionConnectionManager:
    """Owns the transport, protocol state, reconnection and status fan-out."""

    def __init__(self, settings: CompanionConnectionSettings) -> None:
        self._settings = settings

        self._lock = threading.RLock()
        self._status = ConnectionStatus(
            endpoint=settings.endpoint, transport_name=settings.mode
        )

        self._transport: CompanionTransport | None = None
        self._framer = LineFramer()

        self._generation = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._connection_over = threading.Event()
        self._supervisor: threading.Thread | None = None

        self._unacked_pings = 0
        self._caps_deadline: float | None = None
        # Whether the attempt in progress ever reached CONNECTED. Drives the
        # backoff reset below.
        self._reached_connected = False

        self._message_listeners: list[MessageListener] = []
        self._status_listeners: list[StatusListener] = []

        # Debug-only: when each address's press/release was last sent, so a
        # received bitmap can log the round trip. Never used for logic.
        self._press_sent_at: dict[CompanionAddress, float] = {}

        self.surface = DynamicSurface(settings.device_id, self.send)
        self.subscriptions = SubscriptionRegistry(
            on_first_listener=self._subscribe_address,
            on_last_listener=self._unsubscribe_address,
        )

    # --- Public API --------------------------------------------------------

    @property
    def status(self) -> ConnectionStatus:
        with self._lock:
            return self._status

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def is_connected(self) -> bool:
        return self.status.state.is_usable

    def add_message_listener(self, listener: MessageListener) -> None:
        """Register for protocol messages the manager does not consume itself.

        Listeners run on the transport thread and are given the generation the
        message arrived on, so they can discard stale work.
        """
        with self._lock:
            self._message_listeners.append(listener)

    def add_status_listener(self, listener: StatusListener) -> None:
        with self._lock:
            self._status_listeners.append(listener)
        listener(self.status)

    def start(self) -> None:
        """Begin connecting and keep the connection alive until stopped."""
        with self._lock:
            if self._supervisor is not None:
                return
            self._stop.clear()
            self._supervisor = threading.Thread(
                target=self._supervise, name="companion-manager", daemon=True
            )
            self._supervisor.start()

    def stop(self) -> None:
        """Shut down deliberately. Safe to call repeatedly."""
        with self._lock:
            supervisor = self._supervisor
            self._supervisor = None

        self._set_state(ConnectionState.STOPPING)
        self._stop.set()
        self._wake.set()
        self._connection_over.set()

        transport = self._take_transport()
        if transport is not None:
            transport.disconnect()

        if supervisor is not None and supervisor is not threading.current_thread():
            supervisor.join(timeout=6.0)
            if supervisor.is_alive():
                log.warning("Companion manager supervisor did not stop cleanly")

        self._set_state(ConnectionState.DISCONNECTED)

    def update_settings(self, settings: CompanionConnectionSettings) -> None:
        """Replace the endpoint, reconnecting only if something material changed.

        Old transport callbacks cannot affect the new connection because the
        generation advances.
        """
        with self._lock:
            if settings == self._settings:
                return
            log.info(
                "Companion connection settings changed: %s -> %s",
                self._settings.describe(),
                settings.describe(),
            )
            self._settings = settings
            self.surface.set_device_id(settings.device_id)
            self._status = replace(
                self._status,
                endpoint=settings.endpoint,
                transport_name=settings.mode,
                last_error=None,
            )

        self._restart_connection()

    def send(self, payload: bytes) -> bool:
        """Write to Companion if connected. Never blocks."""
        with self._lock:
            transport = self._transport
            usable = self._status.state.is_usable
        if transport is None or not usable:
            return False
        return transport.send(payload)

    def retry_now(self) -> None:
        """Abandon any backoff and try again immediately."""
        with self._lock:
            self._status = replace(self._status, last_error=None)
        self._restart_connection()

    # --- Action-facing API -------------------------------------------------
    #
    # This is everything CompanionButton needs. No action ever sees a socket,
    # a protocol message or a bitmap payload.

    def attach(self, address: CompanionAddress, listener) -> object:
        """Start mirroring a Companion button; returns its cached image if known.

        The first listener on an address creates the Companion subscription;
        later ones share it.
        """
        return self.subscriptions.add_listener(address, listener)

    def detach(self, address: CompanionAddress, listener) -> None:
        """Stop mirroring; removes the subscription if this was the last listener."""
        self.subscriptions.remove_listener(address, listener)

    def detach_everywhere(self, listener) -> None:
        """Remove a listener from every address, for permanent action removal."""
        self.subscriptions.remove_listener_everywhere(listener)

    def key_down(self, address: CompanionAddress) -> bool:
        return self._send_press(address, pressed=True)

    def key_up(self, address: CompanionAddress) -> bool:
        return self._send_press(address, pressed=False)

    def rotate(self, address: CompanionAddress, clockwise: bool) -> bool:
        """Send one rotation tick.

        StreamController discards encoder magnitude before actions see it, so
        one event is exactly one tick (Q1).
        """
        if address.dynamic_page:
            key_index = self.surface.key_index_for(address)
            if key_index is None:
                return False
            return self.send(
                protocol.key_rotate(self.surface.device_id, key_index, clockwise)
            )

        if not self.status.subscriptions_supported:
            return False
        return self.send(protocol.sub_rotate(address.sub_id, clockwise))

    def _send_press(self, address: CompanionAddress, pressed: bool) -> bool:
        with self._lock:
            self._press_sent_at[address] = time.monotonic()

        if address.dynamic_page:
            key_index = self.surface.key_index_for(address)
            if key_index is None:
                return False
            return self.send(
                protocol.key_press(self.surface.device_id, key_index, pressed)
            )

        if not self.status.subscriptions_supported:
            return False
        return self.send(protocol.sub_press(address.sub_id, pressed))

    # --- Subscription hooks (called by the registry) ----------------------

    def _subscribe_address(self, address: CompanionAddress) -> None:
        """Create the Companion-side subscription for a newly watched address."""
        if address.dynamic_page:
            was_registered = self.surface.is_registered
            self.surface.require(address)
            # Requiring an address the surface already covers sends nothing, so
            # Companion never resends that button and a newly visible control
            # would sit on "loading" until the button happened to change. Ask
            # for a refresh instead.
            if was_registered and self.subscriptions.cached_image(address) is None:
                self.surface.refresh()
            return

        if not self.status.state.is_usable:
            return  # resubscribe_all will run once connected

        if not self.status.subscriptions_supported:
            log.warning(
                "Static control %s cannot work: %s",
                address.describe(),
                STATIC_UNSUPPORTED,
            )
            self.subscriptions.set_error(address, STATIC_UNSUPPORTED)
            return

        log.debug("Subscribe %s", address.describe())
        self.send(
            protocol.add_sub(
                address.sub_id,
                bitmap_format=self.status.capabilities.negotiated_bitmap_format,
            )
        )

    def _unsubscribe_address(self, address: CompanionAddress) -> None:
        if address.dynamic_page:
            self.surface.release(address)
            return

        if self.status.state.is_usable and self.status.subscriptions_supported:
            log.debug("Unsubscribe %s", address.describe())
            self.send(protocol.remove_sub(address.sub_id))

    # --- Supervisor --------------------------------------------------------

    def _supervise(self) -> None:
        """Connect, hold the connection alive, then back off and retry."""
        attempt = 0

        while not self._stop.is_set():
            established = self._run_one_connection()

            if self._stop.is_set():
                break

            # A connection that actually worked proves the endpoint is healthy,
            # so the next blip should be retried promptly rather than inheriting
            # the delay from earlier failures. Without this, a long session with
            # occasional drops settles at the maximum backoff forever.
            if established:
                attempt = 0

            if self.status.state.is_terminal:
                # An incompatible Companion or a bad configuration will not fix
                # itself; wait to be poked rather than hammering the endpoint.
                self._wake.wait()
                self._wake.clear()
                attempt = 0
                continue

            delay = self._backoff_delay(attempt)
            attempt += 1
            log.info("Reconnecting to Companion in %.0f seconds", delay)
            if self._wake.wait(delay):
                self._wake.clear()
                attempt = 0

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        delays = constants.RECONNECT_DELAYS_SECONDS
        return delays[min(attempt, len(delays) - 1)]

    def _run_one_connection(self) -> bool:
        """One full connection attempt. Returns whether it reached CONNECTED."""
        with self._lock:
            settings = self._settings
            self._generation += 1
            generation = self._generation
            self._framer.reset()
            self._unacked_pings = 0
            self._caps_deadline = None
            self._reached_connected = False

        self._connection_over.clear()
        self._set_state(ConnectionState.CONNECTING, last_error=None)

        transport = SatelliteTcpTransport(
            settings.host,
            settings.port,
            TransportCallbacks(
                on_connected=lambda: self._on_connected(generation),
                on_data=lambda chunk: self._on_data(chunk, generation),
                on_closed=lambda reason: self._on_closed(reason, generation),
            ),
        )

        with self._lock:
            self._transport = transport

        log.info("Connecting to Companion at %s", transport.description)
        transport.connect()

        try:
            self._heartbeat_loop(generation)
        finally:
            transport.disconnect()
            with self._lock:
                if self._transport is transport:
                    self._transport = None
            self._on_connection_ended()

        with self._lock:
            return self._reached_connected

    def _heartbeat_loop(self, generation: int) -> None:
        """Ping for the life of the connection.

        Not optional: Companion closes a connection that has not pinged for
        about five seconds, so this is what keeps the session open as well as
        what detects a connection that is open but dead.
        """
        while not self._stop.is_set():
            if self._connection_over.wait(constants.PING_INTERVAL_SECONDS):
                return

            if self._generation_changed(generation):
                return

            self._expire_caps_wait(generation)

            state = self.status.state
            if state is not ConnectionState.CONNECTED:
                continue

            if self._unacked_pings > constants.PING_UNACKED_LIMIT:
                log.warning(
                    "Companion stopped answering pings (%d unacked); reconnecting",
                    self._unacked_pings,
                )
                self._fail_connection("Companion stopped responding", generation)
                return

            self._unacked_pings += 1
            if not self.send(protocol.ping()):
                return

    def _expire_caps_wait(self, generation: int) -> None:
        """Complete the handshake if CAPS never arrived.

        Companion may not send CAPS at all. Waiting forever would leave the
        connection stuck in NEGOTIATING, so proceed without subscription
        support rather than hanging.
        """
        deadline = self._caps_deadline
        if deadline is None or time.monotonic() < deadline:
            return
        with self._lock:
            self._caps_deadline = None
        log.warning("No CAPS from Companion; continuing without subscription support")
        self._complete_handshake(generation)

    # --- Transport callbacks (transport thread) ---------------------------

    def _generation_changed(self, generation: int) -> bool:
        with self._lock:
            return generation != self._generation

    def _on_connected(self, generation: int) -> None:
        if self._generation_changed(generation):
            return
        log.info("Companion socket established; awaiting handshake")
        self._set_state(ConnectionState.NEGOTIATING)

    def _on_data(self, chunk: bytes, generation: int) -> None:
        if self._generation_changed(generation):
            return

        try:
            for message in parse_stream(self._framer, chunk):
                if self._generation_changed(generation):
                    return
                self._dispatch(message, generation)
        except Exception:  # noqa: BLE001 - never let parsing kill the transport
            log.error("Failed handling Companion data", exc_info=True)

        dropped = self._framer.take_overflow()
        if dropped:
            log.warning("Discarded %d bytes of oversized Companion input", dropped)

    def _on_closed(self, reason: str | None, generation: int) -> None:
        if self._generation_changed(generation):
            return
        if reason:
            log.warning("Companion connection lost: %s", reason)
        self._set_state(ConnectionState.DISCONNECTED, last_error=reason)
        self._connection_over.set()

    def _fail_connection(self, reason: str, generation: int) -> None:
        if self._generation_changed(generation):
            return
        self._set_state(ConnectionState.DISCONNECTED, last_error=reason)
        self._connection_over.set()

    # --- Protocol handling -------------------------------------------------

    def _dispatch(self, message: SatelliteMessage, generation: int) -> None:
        command = message.command

        if command == protocol.Inbound.BEGIN:
            self._handle_begin(message, generation)
            return
        if command == protocol.Inbound.CAPS:
            self._handle_caps(message, generation)
            return
        if command == protocol.Inbound.PING:
            self.send(protocol.pong())
            return
        if command == protocol.Inbound.PONG:
            self._unacked_pings = 0
            return
        if command == protocol.Inbound.KEY_STATE:
            self._handle_key_state(message, generation)
            return
        if command == protocol.Inbound.SUB_STATE:
            self._handle_sub_state(message, generation)
            return
        if command == protocol.Inbound.ADD_SUB:
            self._handle_add_sub_reply(message)
            return
        if command in (
            protocol.Inbound.KEY_PRESS,
            protocol.Inbound.KEY_ROTATE,
            protocol.Inbound.SUB_PRESS,
            protocol.Inbound.SUB_ROTATE,
        ):
            self._handle_input_reply(message)
            return
        if command == protocol.Inbound.BRIGHTNESS:
            # Companion pushes surface brightness; StreamController owns
            # brightness, so this is acknowledged by ignoring it.
            return
        if command == protocol.Inbound.KEYS_CLEAR:
            log.debug("Companion cleared dynamic keys")
            self.subscriptions.invalidate_dynamic()
            return

        self._notify_message(message, generation)

    # --- Imagery -----------------------------------------------------------

    def _handle_key_state(self, message: SatelliteMessage, generation: int) -> None:
        """Route a dynamic-surface image to its address."""
        key_index = message.integer("KEY")
        if key_index is None:
            return

        address = self.surface.address_for_key_index(key_index)
        if address is None:
            # Arrived before our ADD-DEVICE, or outside the registered grid.
            log.debug(
                "KEY-STATE key=%s dropped: surface geometry is %s",
                key_index,
                self.surface.registered_geometry,
            )
            return

        self._deliver_bitmap(address, message.text("BITMAP"), generation)

    def _handle_sub_state(self, message: SatelliteMessage, generation: int) -> None:
        """Route a static-subscription image to its address."""
        sub_id = message.text("SUBID")
        if not sub_id:
            return
        try:
            address = CompanionAddress.from_sub_id(sub_id)
        except CompanionConfigError as exc:
            log.debug("Ignoring SUB-STATE with bad SUBID: %s", exc)
            return

        self._deliver_bitmap(address, message.text("BITMAP"), generation)

    def _deliver_bitmap(
        self, address: CompanionAddress, payload: str | None, generation: int
    ) -> None:
        """Decode and fan out, or drop quietly if the payload is unusable.

        Decoding happens here, once per received image, rather than per
        listener. A failed decode is not fatal: the previous
        image stays on screen until Companion sends something valid.
        """
        if not payload:
            return

        image = decode_button_image(
            payload, self.status.capabilities.negotiated_bitmap_format
        )
        if image is None:
            return

        with self._lock:
            sent_at = self._press_sent_at.pop(address, None)
        if sent_at is not None:
            log.debug(
                "Companion feedback for %s took %.0f ms round trip",
                address.describe(),
                (time.monotonic() - sent_at) * 1000,
            )

        if self._generation_changed(generation):
            return

        self.subscriptions.deliver_image(address, image, generation)

    def _handle_input_reply(self, message: SatelliteMessage) -> None:
        """Log a rejected input event.

        Companion acknowledges every press and rotation with ``OK=1``, or
        rejects it with ``ERROR=1 MESSAGE=...``. Silently dropping the error
        would make a mis-addressed button look like a dead one.
        """
        if not message.flag("ERROR"):
            return
        log.warning(
            "Companion rejected %s: %s",
            message.command,
            message.text("MESSAGE") or "no reason given",
        )

    def _handle_add_sub_reply(self, message: SatelliteMessage) -> None:
        """Surface a rejected subscription instead of leaving a blank key."""
        if not message.flag("ERROR"):
            return

        detail = message.text("MESSAGE") or "Companion rejected the subscription"
        sub_id = message.text("SUBID")

        if not sub_id:
            log.warning("Companion rejected a subscription: %s", detail)
            return

        log.warning("Companion rejected subscription %s: %s", sub_id, detail)
        try:
            address = CompanionAddress.from_sub_id(sub_id)
        except CompanionConfigError:
            return
        self.subscriptions.set_error(address, detail)

    def _handle_begin(self, message: SatelliteMessage, generation: int) -> None:
        api_version = message.text("ApiVersion")
        companion_version = message.text("CompanionVersion")

        if not api_version:
            self._reject_version(None, generation)
            return

        if _compare_versions(api_version, constants.MINIMUM_SATELLITE_API_VERSION) < 0:
            self._reject_version(api_version, generation)
            return

        log.info(
            "Companion %s speaking Satellite API %s",
            companion_version or "(unknown version)",
            api_version,
        )

        with self._lock:
            self._status = replace(
                self._status,
                api_version=api_version,
                companion_version=companion_version,
                capabilities=CompanionCapabilities(),
            )
            # Companion usually sends CAPS immediately after BEGIN; if it does
            # not, the heartbeat loop completes the handshake without it.
            self._caps_deadline = time.monotonic() + constants.CAPS_TIMEOUT_SECONDS

    def _reject_version(self, api_version: str | None, generation: int) -> None:
        """Refuse an unusable Companion without entering a reconnect loop."""
        required = constants.MINIMUM_SATELLITE_API_VERSION
        detail = (
            f"Companion reports Satellite API {api_version}, but this plugin "
            f"requires {required} or newer"
            if api_version
            else "Companion did not report a Satellite API version"
        )
        log.error("%s", detail)

        self._set_state(ConnectionState.INCOMPATIBLE, last_error=detail)
        self._connection_over.set()

    def _handle_caps(self, message: SatelliteMessage, generation: int) -> None:
        text_params = {k: v for k, v in message.params.items() if isinstance(v, str)}
        capabilities = CompanionCapabilities.from_caps_params(text_params)

        with self._lock:
            self._caps_deadline = None
            self._status = replace(self._status, capabilities=capabilities)

        log.info("Companion capabilities: %s", capabilities.describe())
        self._complete_handshake(generation)

    def _complete_handshake(self, generation: int) -> None:
        if self._generation_changed(generation):
            return
        if self.status.state is not ConnectionState.NEGOTIATING:
            return

        self._unacked_pings = 0
        with self._lock:
            self._reached_connected = True
        self._set_state(ConnectionState.CONNECTED, last_error=None)
        log.info("Companion connected via %s", self.status.endpoint)

        # Ping at once: Companion drops a connection that stays silent for ~5s.
        self.send(protocol.ping())

        self._restore_after_connect()

    def _restore_after_connect(self) -> None:
        """Rebuild everything Companion has no memory of.

        Order matters: the surface must exist before its imagery can be
        interpreted, and cached images must be distrusted before new ones
        arrive so nothing stale is left on screen if Companion goes quiet.
        """
        capabilities = self.status.capabilities

        # Anything cached predates this connection; toggle and feedback states
        # may have changed while we were away.
        self.subscriptions.invalidate_all()

        # Recreates the device with recalculated dimensions.
        self.surface.on_connected(capabilities)

        # Recreates every static subscription.
        self.subscriptions.resubscribe_all()

    def _on_connection_ended(self) -> None:
        """Forget connection-scoped state once a connection is over."""
        self.surface.on_disconnected()
        self.subscriptions.invalidate_all()

    # --- Plumbing ----------------------------------------------------------

    def _restart_connection(self) -> None:
        """Drop the current connection so the supervisor rebuilds it."""
        with self._lock:
            self._generation += 1  # fences every in-flight callback
            transport = self._transport
            self._transport = None

        if transport is not None:
            transport.disconnect()

        self._connection_over.set()
        self._wake.set()

    def _take_transport(self) -> CompanionTransport | None:
        with self._lock:
            transport, self._transport = self._transport, None
            return transport

    def _set_state(
        self, state: ConnectionState, last_error: str | None | _Unset = _UNSET
    ) -> None:
        with self._lock:
            if self._status.state is state and last_error is _UNSET:
                return
            previous = self._status.state
            self._status = replace(
                self._status,
                state=state,
                generation=self._generation,
                **({} if last_error is _UNSET else {"last_error": last_error}),
            )
            snapshot = self._status
            listeners = list(self._status_listeners)

        if previous is not state:
            log.debug("Companion state %s -> %s", previous.value, state.value)

        # Listeners are called outside the lock: they touch the UI, and holding
        # a lock across that risks deadlock.
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001
                log.error("Status listener raised", exc_info=True)

    def _notify_message(self, message: SatelliteMessage, generation: int) -> None:
        with self._lock:
            listeners = list(self._message_listeners)

        for listener in listeners:
            try:
                listener(message, generation)
            except Exception:  # noqa: BLE001
                log.error(
                    "Message listener raised for %s", message.command, exc_info=True
                )
