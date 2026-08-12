"""The Companion Button action.

One instance mirrors a single Companion button onto a StreamController key or
dial. It owns coordinates, lifecycle and rendering only — never sockets,
reconnect loops, protocol parsing or the global image cache.

Threading note: Companion imagery arrives on the connection manager's transport
thread. `set_media()` is safe to call from there because StreamController's
render pipeline marshals its own GTK work internally, so imagery is passed
straight through rather than being queued onto the main loop, which would add
latency to press-state changes.
"""

from __future__ import annotations

import threading
import time

from loguru import logger as log

from GtkHelper.GenerativeUI.SpinRow import SpinRow
from GtkHelper.GenerativeUI.SwitchRow import SwitchRow
from src.backend.PluginManager.InputBases import DialAction, KeyAction

from ..companion import constants
from ..companion.manager import STATIC_UNSUPPORTED
from ..companion.models import (
    CompanionAddress,
    CompanionConfigError,
    ConnectionState,
)

# Settings keys, and the defaults a freshly placed action starts with.
DEFAULT_SETTINGS = {
    "dynamic_page": True,
    "page": 1,
    "row": 0,
    "column": 0,
}


class CompanionButton(KeyAction, DialAction):
    """A StreamController control that mirrors and operates a Companion button.

    Inherits from both ``KeyAction`` and ``DialAction`` so one action can serve
    ``Input.Key`` and ``Input.Dial``. Both derive from ``ActionCore`` and
    cooperate through ``super().__init__``, so a single MRO chain registers the
    key event assigners and the dial event assigners.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.has_configuration = True

        # Companion requires tightly paired DOWN/UP semantics. Letting the user
        # remap these independently could produce an UP without a DOWN and
        # leave a Companion button stuck down.
        self.allow_event_configuration = False

        self._address: CompanionAddress | None = None
        self._config_error: str | None = None
        self._attached = False
        # True while the key is showing Companion imagery rather than a status
        # message. Used to avoid re-clearing labels on every frame.
        self._showing_companion_image = False

        # StreamController dispatches DOWN and UP on independently spawned
        # threads with no ordering guarantee between them (own_actions_
        # event_callback_threaded creates a fresh thread per event). On a
        # fast tap, the UP thread can occasionally reach the socket before
        # the DOWN thread, so Companion would see PRESSED=0 before PRESSED=1
        # and its feedback would latch one toggle out of phase with the
        # physical key until another lucky re-ordering corrected it. This
        # counter pair plus condition variable makes an UP send always wait
        # for its paired DOWN send to happen first, regardless of which
        # thread the OS schedules first.
        self._order_cv = threading.Condition()
        self._down_count = 0
        self._up_count = 0

        self._build_config_ui()

    # --- Configuration UI --------------------------------------------------

    def _text(self, key: str, fallback: str = "") -> str:
        """Look up a localized string, tolerating a missing locale file."""
        try:
            return self.get_translation(key, fallback or key)
        except Exception:  # noqa: BLE001
            return fallback or key

    def _build_config_ui(self) -> None:
        """Coordinates are 0-based, matching Companion's wire convention."""
        self.dynamic_page_row = SwitchRow(
            action_core=self,
            var_name="dynamic_page",
            default_value=True,
            title=self._text("action.dynamic-page.title"),
            subtitle=self._text("action.dynamic-page.subtitle"),
            on_change=self._on_dynamic_page_changed,
        )

        self.page_row = SpinRow(
            action_core=self,
            var_name="page",
            default_value=1,
            min=1,
            max=constants.MAX_PAGE,
            step=1,
            digits=0,
            title=self._text("action.page.title"),
            on_change=self._on_coordinate_changed,
        )

        self.row_row = SpinRow(
            action_core=self,
            var_name="row",
            default_value=0,
            min=0,
            max=constants.MAX_SURFACE_ROWS - 1,
            step=1,
            digits=0,
            title=self._text("action.row.title"),
            on_change=self._on_coordinate_changed,
        )

        self.column_row = SpinRow(
            action_core=self,
            var_name="column",
            default_value=0,
            min=0,
            max=constants.MAX_SURFACE_COLUMNS - 1,
            step=1,
            digits=0,
            title=self._text("action.column.title"),
            on_change=self._on_coordinate_changed,
        )

    def _on_dynamic_page_changed(self, _widget, new_value, _old_value) -> None:
        # The page field is meaningless in dynamic mode.
        self._update_page_row_sensitivity(bool(new_value))
        self._on_coordinate_changed(None, None, None)

    def _update_page_row_sensitivity(self, dynamic: bool) -> None:
        try:
            self.page_row.widget.set_sensitive(not dynamic)
        except Exception:  # noqa: BLE001 - widget may not exist yet
            pass

    def _on_coordinate_changed(self, _widget, _new_value, _old_value) -> None:
        """Re-address atomically: drop the old subscription, take the new one."""
        self._attach()

    # --- Plumbing ----------------------------------------------------------

    @property
    def manager(self):
        return getattr(self.plugin_base, "manager", None)

    def _read_address(self) -> CompanionAddress | None:
        """Build the address from settings, recording any validation failure."""
        settings = {**DEFAULT_SETTINGS, **(self.get_settings() or {})}
        try:
            address = CompanionAddress.from_settings(settings)
        except CompanionConfigError as exc:
            self._config_error = str(exc)
            log.warning(f"Companion Button has invalid settings: {exc}")
            return None

        self._config_error = None
        return address

    # --- Lifecycle ---------------------------------------------------------

    def on_ready(self) -> None:
        """Attach to the connection manager and draw the current state.

        Always redraws: an internally cached image is no evidence that the
        physical key currently shows it.
        """
        settings = {**DEFAULT_SETTINGS, **(self.get_settings() or {})}
        self._update_page_row_sensitivity(bool(settings.get("dynamic_page", True)))
        self._attach()

    def on_update(self) -> None:
        # ActionCore's default forwards to on_ready; being explicit keeps the
        # redraw-on-every-update contract obvious.
        self.on_ready()

    def on_remove(self) -> None:
        """The user deleted this action; leave nothing behind."""
        self._detach(everywhere=True)

    def on_removed_from_cache(self) -> None:
        """StreamController evicted this action; stop listening."""
        self._detach()

    def _attach(self) -> None:
        manager = self.manager
        if manager is None:
            return

        # The action may have just (re)appeared with labels restored from the
        # page config, so the one-time clear has to happen again.
        self._showing_companion_image = False

        address = self._read_address()

        # Re-addressing must be atomic from the user's point of view: drop the
        # old subscription before taking the new one, never hold both.
        if self._attached and address != self._address:
            self._detach()

        if address is None:
            self._render_state_text(self._text("key.invalid-config"), error=True)
            return

        log.debug(
            f"Companion attach {address.describe()} "
            f"(was {self._address.describe() if self._address else 'none'}, "
            f"attached={self._attached})"
        )
        self._address = address
        if not self._attached:
            cached = manager.attach(address, self._on_companion_image)
            self._attached = True
        else:
            cached = manager.subscriptions.cached_image(address)

        log.debug(
            f"Companion attach result for {address.describe()}: "
            f"{'cached image' if cached is not None else 'no cached image'}"
        )
        if cached is not None:
            self._render_image(cached)
        else:
            self._render_connection_state()

    def _detach(self, everywhere: bool = False) -> None:
        manager = self.manager
        if manager is None:
            self._attached = False
            return

        if everywhere:
            manager.detach_everywhere(self._on_companion_image)
        elif self._attached and self._address is not None:
            manager.detach(self._address, self._on_companion_image)

        self._attached = False

    # --- Rendering ---------------------------------------------------------

    def _on_companion_image(self, address: CompanionAddress, image) -> None:  # noqa: D401
        """Receive one Companion image. Runs on the transport thread.

        ``image`` is ``None`` when the manager no longer trusts the cached
        state — after KEYS-CLEAR, a reconnect, or a rejected subscription — in
        which case the control falls back to showing its connection state
        rather than stale imagery.
        """
        if address != self._address:
            log.debug(
                f"Companion image for {address.describe()} ignored; "
                f"this action now represents "
                f"{self._address.describe() if self._address else 'nothing'}"
            )
            return  # a late delivery for an address we no longer represent

        log.debug(
            f"Companion image received for {address.describe()}: "
            f"{'cleared' if image is None else 'image'}"
        )

        if image is None:
            self._render_connection_state()
        else:
            self._render_image(image, received_at=time.monotonic())

    def _render_image(self, image, received_at: float | None = None) -> None:
        """Put Companion's rendered image on the key.

        Unconditional by design: never skipped because the image matches what
        was drawn before, since the physical display may have changed while the
        page was inactive.
        """
        if not self.on_ready_called:
            log.debug("Companion render skipped: action not ready yet")
            return
        if not self.get_is_present():
            log.debug("Companion render skipped: action not present")
            return

        try:
            t0 = time.monotonic()
            # Clearing the labels and the error state is only needed when
            # switching away from a status message. Each of these defaults to
            # update=True, and every update composites the key and queues its
            # own hardware write - so doing them per frame cost five deck
            # writes for every Companion image instead of one. Companion's
            # image is the authoritative visual content and a local label would
            # obscure it, but clearing them once is enough.
            if not self._showing_companion_image:
                self.hide_error()
                self.set_top_label("", update=False)
                self.set_center_label("", update=False)
                self.set_bottom_label("", update=False)
                self._showing_companion_image = True
            t1 = time.monotonic()
            # The single update that reaches the deck.
            self.set_media(image=image)
            t2 = time.monotonic()
            if received_at is not None:
                # loguru uses {} formatting, not %-style
                log.debug(
                    f"Companion render timing: queue={((t0 - received_at) * 1000):.1f}ms "
                    f"labels={((t1 - t0) * 1000):.1f}ms "
                    f"set_media={((t2 - t1) * 1000):.1f}ms "
                    f"total={((t2 - received_at) * 1000):.1f}ms"
                )
        except Exception as exc:  # noqa: BLE001
            log.error(f"Failed rendering Companion image: {exc}")

    def _render_state_text(self, text: str, error: bool = False) -> None:
        """Show a short status instead of imagery, before Companion provides any."""
        if not self.on_ready_called or not self.get_is_present():
            return

        try:
            self._showing_companion_image = False
            self.set_media(image=None, update=False)
            self.set_center_label(text)
            if error:
                self.show_error()
            else:
                self.hide_error()
        except Exception as exc:  # noqa: BLE001
            log.error(f"Failed rendering Companion state: {exc}")

    def _render_connection_state(self) -> None:
        """Make the control's situation understandable."""
        if self._config_error is not None:
            self._render_state_text(self._text("key.invalid-config"), error=True)
            return

        manager = self.manager
        if manager is None:
            self._render_state_text(self._text("key.no-plugin"), error=True)
            return

        status = manager.status
        state = status.state

        if state is ConnectionState.INCOMPATIBLE:
            self._render_state_text(self._text("key.incompatible"), error=True)
            return
        if state is ConnectionState.CONFIG_ERROR:
            self._render_state_text(self._text("key.config-error"), error=True)
            return

        # A static control on a Companion without subscription support cannot
        # work, and must say so rather than silently following the active page.
        if (
            state.is_usable
            and self._address is not None
            and not self._address.dynamic_page
            and not status.subscriptions_supported
        ):
            log.warning(f"Companion Button: {STATIC_UNSUPPORTED}")
            self._render_state_text(self._text("key.static-unsupported"), error=True)
            return

        if state.is_usable:
            self._render_state_text(self._text("key.loading"))
        else:
            self._render_state_text(self._text("key.offline"))

    def on_connection_status_changed(self, status) -> None:
        """Called by the plugin when the shared connection changes state."""
        if not self._attached or self._address is None:
            return

        manager = self.manager
        cached = (
            manager.subscriptions.cached_image(self._address)
            if manager is not None
            else None
        )
        if cached is not None and status.state.is_usable:
            self._render_image(cached)
        else:
            self._render_connection_state()

    # --- Input -------------------------------------------------------------
    #
    # Each handler hands the event off and returns. Nothing here waits on the
    # socket or on an image response, and no pressed state is faked locally —
    # the pressed image must come from Companion.

    def _forward(self, action: str) -> None:
        manager = self.manager
        if manager is None or self._address is None:
            return

        if action == "down":
            with self._order_cv:
                self._down_count += 1
                manager.key_down(self._address)
                self._order_cv.notify_all()
        elif action == "up":
            # Wait for the paired DOWN send to actually happen first. A
            # physical release always follows a physical press, so the DOWN
            # thread is guaranteed to already be running (or about to run);
            # the timeout only guards against ever hanging this thread if
            # something upstream truly dropped the DOWN event.
            with self._order_cv:
                have_pair = self._order_cv.wait_for(
                    lambda: self._down_count > self._up_count, timeout=1.0
                )
                if not have_pair:
                    log.warning(
                        "Companion key_up sent without a matching key_down "
                        "observed for %s; sending anyway",
                        self._address.describe() if self._address else "?",
                    )
                self._up_count += 1
                manager.key_up(self._address)
                self._order_cv.notify_all()
        elif action == "cw":
            manager.rotate(self._address, clockwise=True)
        elif action == "ccw":
            manager.rotate(self._address, clockwise=False)

    # StreamController's EventAssigner always calls handlers with the event's
    # data payload, even though the InputBases signatures omit it, so every
    # handler must tolerate the extra argument. (StreamController's own
    # DialAction.on_dial_short_up hits the same TypeError without this.)

    def on_key_down(self, *_data) -> None:
        self._forward("down")

    def on_key_up(self, *_data) -> None:
        self._forward("up")

    def on_key_short_up(self, *_data) -> None:
        # Companion gets DOWN/UP; SHORT_UP would double-send.
        pass

    def on_key_hold_start(self, *_data) -> None:
        pass

    def on_key_hold_stop(self, *_data) -> None:
        pass

    def on_dial_down(self, *_data) -> None:
        self._forward("down")

    def on_dial_up(self, *_data) -> None:
        self._forward("up")

    def on_dial_short_up(self, *_data) -> None:
        pass

    def on_dial_hold_start(self, *_data) -> None:
        pass

    def on_dial_hold_stop(self, *_data) -> None:
        pass

    def on_dial_turn_cw(self, *_data) -> None:
        self._forward("cw")

    def on_dial_turn_ccw(self, *_data) -> None:
        self._forward("ccw")

    def on_dial_short_touch_press(self, *_data) -> None:
        pass

    def on_dial_long_touch_press(self, *_data) -> None:
        pass
