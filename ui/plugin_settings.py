"""Plugin-wide settings UI.

Connection configuration belongs here, not on every button.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from loguru import logger as log  # noqa: E402

from ..companion import constants  # noqa: E402
from ..companion.models import ConnectionState  # noqa: E402

# What each connection state should read as in the UI.
STATE_KEYS = {
    ConnectionState.DISCONNECTED: "status.disconnected",
    ConnectionState.CONNECTING: "status.connecting",
    ConnectionState.NEGOTIATING: "status.negotiating",
    ConnectionState.CONNECTED: "status.connected",
    ConnectionState.INCOMPATIBLE: "status.incompatible",
    ConnectionState.CONFIG_ERROR: "status.config-error",
    ConnectionState.STOPPING: "status.stopping",
}


class CompanionSettingsArea:
    """Builds and maintains the plugin settings page."""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._locale = plugin.locale_manager
        self._status_row: Adw.ActionRow | None = None
        self._details_row: Adw.ActionRow | None = None

    def _text(self, key: str, fallback: str = "") -> str:
        try:
            return self._locale.get(key, fallback or key)
        except Exception:  # noqa: BLE001
            return fallback or key

    # --- Construction ------------------------------------------------------

    def build(self) -> Adw.PreferencesGroup:
        settings = self.plugin.get_settings()

        group = Adw.PreferencesGroup()
        group.set_title(self._text("settings.group.title"))
        group.set_description(self._text("settings.group.description"))

        self.host_row = Adw.EntryRow(title=self._text("settings.host.title"))
        self.host_row.set_text(
            str(settings.get("satellite_host", constants.DEFAULT_HOST))
        )
        self.host_row.connect("apply", self._on_endpoint_changed)
        self.host_row.set_show_apply_button(True)
        group.add(self.host_row)

        self.port_row = Adw.SpinRow(
            title=self._text("settings.port.title"),
            adjustment=Gtk.Adjustment(
                lower=1,
                upper=65535,
                step_increment=1,
                page_increment=100,
                value=float(settings.get("satellite_port", constants.DEFAULT_PORT)),
            ),
        )
        self.port_row.connect("changed", self._on_endpoint_changed)
        group.add(self.port_row)

        self._status_row = Adw.ActionRow(title=self._text("settings.status.title"), subtitle=self._text("settings.unknown"))
        group.add(self._status_row)

        self._details_row = Adw.ActionRow(title=self._text("settings.companion.title"), subtitle=self._text("settings.unknown"))
        group.add(self._details_row)

        identity_row = Adw.ActionRow(
            title=self._text("settings.device-id.title"),
            subtitle=self.plugin.get_device_id(),
        )
        group.add(identity_row)

        self.debug_row = Adw.SwitchRow(
            title=self._text("settings.debug.title"),
            subtitle=self._text("settings.debug.subtitle"),
        )
        self.debug_row.set_active(bool(settings.get("debug_logging", False)))
        self.debug_row.connect("notify::active", self._on_debug_toggled)
        group.add(self.debug_row)

        reconnect = Gtk.Button(label=self._text("settings.reconnect"))
        reconnect.set_margin_top(8)
        reconnect.connect("clicked", self._on_reconnect_clicked)
        group.add(reconnect)

        self.update_status(self.plugin.manager.status)
        return group

    # --- Events ------------------------------------------------------------

    def _on_endpoint_changed(self, *_args) -> None:
        host = self.host_row.get_text().strip()
        port = int(self.port_row.get_value())

        settings = self.plugin.get_settings()
        if (
            settings.get("satellite_host") == host
            and settings.get("satellite_port") == port
        ):
            return

        settings["satellite_host"] = host
        settings["satellite_port"] = port
        self.plugin.set_settings(settings)

        log.info(f"Companion endpoint changed to {host}:{port}")
        self.plugin.apply_connection_settings()

    def _on_debug_toggled(self, *_args) -> None:
        enabled = self.debug_row.get_active()
        settings = self.plugin.get_settings()
        settings["debug_logging"] = enabled
        self.plugin.set_settings(settings)
        self.plugin.apply_debug_logging(enabled)

    def _on_reconnect_clicked(self, *_args) -> None:
        self.plugin.manager.retry_now()

    # --- Status ------------------------------------------------------------

    def update_status(self, status) -> None:
        """Refresh the status rows. Safe to call from any thread."""
        if self._status_row is None:
            return
        GLib.idle_add(self._apply_status, status)

    def _apply_status(self, status) -> bool:
        label = self._text(STATE_KEYS.get(status.state, ""), status.state.value)
        if status.last_error:
            label = f"{label} — {status.last_error}"
        self._status_row.set_subtitle(label)

        details = []
        if status.companion_version:
            details.append(f"Companion {status.companion_version}")
        if status.api_version:
            details.append(f"Satellite API {status.api_version}")
        if status.state.is_usable:
            capabilities = status.capabilities
            details.append(
                self._text(
                    "status.subscriptions.supported"
                    if capabilities.subscriptions
                    else "status.subscriptions.unsupported"
                )
            )
            details.append(f"bitmaps: {capabilities.negotiated_bitmap_format}")

        self._details_row.set_subtitle(", ".join(details) if details else self._text("settings.unknown"))
        return False  # do not repeat
