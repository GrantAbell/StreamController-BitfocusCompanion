"""The Companion Page action.

Moves the plugin's virtual surface to the next or previous Companion page, so
that every dynamic-page control follows along.

This exists because Companion gives each surface its own current page. Browsing
pages in the web UI's Buttons tab is an editor view and moves no surface, so
without a control like this one the only way to page a StreamController layout
was through Companion's own surface settings.

Unlike `CompanionButton`, this action mirrors nothing: Companion has no imagery
for "the page key of a satellite surface", so the key draws its own label and
otherwise reports connection state.
"""

from __future__ import annotations

from loguru import logger as log

from GtkHelper.ComboRow import SimpleComboRowItem
from GtkHelper.GenerativeUI.ComboRow import ComboRow
from src.backend.PluginManager.InputBases import DialAction, KeyAction

from ..companion.models import ConnectionState

DIRECTION_NEXT = "next"
DIRECTION_PREVIOUS = "previous"

DEFAULT_SETTINGS = {
    "direction": DIRECTION_NEXT,
}


class CompanionPage(KeyAction, DialAction):
    """A StreamController control that pages the Companion surface."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.has_configuration = True

        # A page change is a single edge, not a press/release pair, so unlike
        # CompanionButton there is nothing here that could be left half-sent.
        # Event configuration is still disabled to keep both actions behaving
        # the same way in the editor.
        self.allow_event_configuration = False

        self._holding_navigation = False

        self._build_config_ui()

    # --- Configuration UI --------------------------------------------------

    def _text(self, key: str, fallback: str = "") -> str:
        """Look up a localized string, tolerating a missing locale file."""
        try:
            return self.get_translation(key, fallback or key)
        except Exception:  # noqa: BLE001
            return fallback or key

    def _build_config_ui(self) -> None:
        # Values are stored, labels are shown: the setting must survive both
        # translation and relabelling.
        self.direction_row = ComboRow(
            action_core=self,
            var_name="direction",
            default_value=DIRECTION_NEXT,
            items=[
                SimpleComboRowItem(
                    DIRECTION_NEXT, self._text("action.page-direction.next")
                ),
                SimpleComboRowItem(
                    DIRECTION_PREVIOUS, self._text("action.page-direction.previous")
                ),
            ],
            title=self._text("action.page-direction.title"),
            subtitle=self._text("action.page-direction.subtitle"),
            on_change=self._on_direction_changed,
        )

    def _on_direction_changed(self, _widget, _new_value, _old_value) -> None:
        self._render_connection_state()

    # --- Plumbing ----------------------------------------------------------

    @property
    def manager(self):
        return getattr(self.plugin_base, "manager", None)

    def _forward_direction(self) -> bool:
        """True when this control moves to the next page."""
        settings = {**DEFAULT_SETTINGS, **(self.get_settings() or {})}
        return settings.get("direction", DIRECTION_NEXT) != DIRECTION_PREVIOUS

    # --- Lifecycle ---------------------------------------------------------

    def on_ready(self) -> None:
        self._hold_navigation()
        self._render_connection_state()

    def on_update(self) -> None:
        self.on_ready()

    def on_remove(self) -> None:
        self._release_navigation()

    def on_removed_from_cache(self) -> None:
        self._release_navigation()

    def _hold_navigation(self) -> None:
        """Ask for the surface to be registered while this control exists.

        Without a registered device Companion has nothing to resolve CHANGE-PAGE
        against, which would make this key silently inert on any layout that has
        no dynamic-page buttons of its own.
        """
        manager = self.manager
        if manager is None or self._holding_navigation:
            return
        manager.hold_page_navigation(self)
        self._holding_navigation = True

    def _release_navigation(self) -> None:
        manager = self.manager
        if manager is None or not self._holding_navigation:
            self._holding_navigation = False
            return
        manager.release_page_navigation(self)
        self._holding_navigation = False

    # --- Rendering ---------------------------------------------------------

    def _render_state_text(self, text: str, error: bool = False) -> None:
        if not self.on_ready_called or not self.get_is_present():
            return

        try:
            self.set_media(image=None, update=False)
            self.set_center_label(text)
            if error:
                self.show_error()
            else:
                self.hide_error()
        except Exception as exc:  # noqa: BLE001
            log.error(f"Failed rendering Companion page control: {exc}")

    def _render_connection_state(self) -> None:
        manager = self.manager
        if manager is None:
            self._render_state_text(self._text("key.no-plugin"), error=True)
            return

        state = manager.status.state

        if state is ConnectionState.INCOMPATIBLE:
            self._render_state_text(self._text("key.incompatible"), error=True)
            return
        if state is ConnectionState.CONFIG_ERROR:
            self._render_state_text(self._text("key.config-error"), error=True)
            return
        if not state.is_usable:
            self._render_state_text(self._text("key.offline"))
            return

        self._render_state_text(
            self._text(
                "key.page-next" if self._forward_direction() else "key.page-previous"
            )
        )

    def on_connection_status_changed(self, _status) -> None:
        """Called by the plugin when the shared connection changes state."""
        # A new connection means a newly registered surface, so the hold has to
        # be re-asserted for layouts whose only Companion control is this one.
        self._hold_navigation()
        self._render_connection_state()

    # --- Input -------------------------------------------------------------

    def _change_page(self) -> None:
        manager = self.manager
        if manager is None:
            return

        forward = self._forward_direction()
        if not manager.change_page(forward):
            log.debug(
                f"Companion page {'next' if forward else 'previous'} not sent; "
                f"the surface is not registered"
            )

    # StreamController's EventAssigner always calls handlers with the event's
    # data payload, even though the InputBases signatures omit it, so every
    # handler must tolerate the extra argument.

    def on_key_down(self, *_data) -> None:
        # On press rather than release, matching how Companion's own surfaces
        # treat their page buttons.
        self._change_page()

    def on_key_up(self, *_data) -> None:
        pass

    def on_key_short_up(self, *_data) -> None:
        pass

    def on_key_hold_start(self, *_data) -> None:
        pass

    def on_key_hold_stop(self, *_data) -> None:
        pass

    def on_dial_down(self, *_data) -> None:
        self._change_page()

    def on_dial_up(self, *_data) -> None:
        pass

    def on_dial_short_up(self, *_data) -> None:
        pass

    def on_dial_hold_start(self, *_data) -> None:
        pass

    def on_dial_hold_stop(self, *_data) -> None:
        pass

    def on_dial_turn_cw(self, *_data) -> None:
        # Rotation is inherently bidirectional, so it ignores the configured
        # direction and pages the way it was turned.
        manager = self.manager
        if manager is not None:
            manager.change_page(True)

    def on_dial_turn_ccw(self, *_data) -> None:
        manager = self.manager
        if manager is not None:
            manager.change_page(False)

    def on_dial_short_touch_press(self, *_data) -> None:
        pass

    def on_dial_long_touch_press(self, *_data) -> None:
        pass
