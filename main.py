"""Bitfocus Companion plugin for StreamController.

Owns plugin registration, plugin-wide settings, the shared connection manager
and the action holder. All Companion protocol work lives under
``companion/`` and never touches StreamController APIs.
"""

import logging
import secrets

from loguru import logger as log

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.PluginManager.PluginBase import PluginBase
from src.Signals import Signals

import globals as gl

from .actions.companion_button import CompanionButton
from .actions.companion_page import CompanionPage
from . import companion as _companion_package
from .companion import constants
from .companion.manager import CompanionConnectionManager
from .companion.models import CompanionConfigError, CompanionConnectionSettings
from .ui.plugin_settings import CompanionSettingsArea


class _LoguruBridge(logging.Handler):
    """Forwards stdlib logging records into StreamController's loguru logger.

    Everything under `companion/` uses stdlib logging so it stays testable
    without StreamController. This hands those records to loguru so they appear
    in the application log with the same formatting as the rest of the app.
    """

    _LEVELS = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = self._LEVELS.get(record.levelno, "INFO")
            log.opt(exception=record.exc_info).log(level, record.getMessage())
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


# The companion package's logger name depends on how the plugin was imported:
# "companion.*" standalone, "plugins.com_grantabell_companion.companion.*" under
# StreamController. Deriving it keeps both working.
COMPANION_LOGGER_NAME = _companion_package.__name__


def _install_logging_bridge() -> None:
    companion_logger = logging.getLogger(COMPANION_LOGGER_NAME)
    if any(isinstance(h, _LoguruBridge) for h in companion_logger.handlers):
        return
    companion_logger.addHandler(_LoguruBridge())
    # Records are delivered to loguru here; letting them also reach the root
    # logger would print each line twice.
    companion_logger.propagate = False


class CompanionPlugin(PluginBase):
    def __init__(self):
        super().__init__(use_legacy_locale=False)

        self.has_plugin_settings = True

        # Ensure a persistent device identity exists before anything can try to
        # connect. Generated once and reused across Companion restarts,
        # StreamController restarts and plugin reloads.
        self._ensure_settings_defaults()

        # One connection shared by every action.
        # Route the Companion package's stdlib logging into StreamController's
        # logger before anything can emit, so protocol logs land in the app log
        # rather than bare stdout.
        _install_logging_bridge()
        self.apply_debug_logging(bool(self.get_settings().get("debug_logging", False)))

        self.manager = CompanionConnectionManager(self._connection_settings())

        # Built before the status listener is registered: add_status_listener
        # invokes the listener immediately with the current status.
        self._settings_area = CompanionSettingsArea(self)
        self.manager.add_status_listener(self._on_connection_status)

        self.companion_button_holder = ActionHolder(
            plugin_base=self,
            action_core=CompanionButton,
            action_id_suffix="CompanionButton",
            action_name=self.locale_manager.get(
                "actions.companion-button.name", "Companion Button"
            ),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.SUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
            },
        )
        self.add_action_holder(self.companion_button_holder)

        self.companion_page_holder = ActionHolder(
            plugin_base=self,
            action_core=CompanionPage,
            action_id_suffix="CompanionPage",
            action_name=self.locale_manager.get(
                "actions.companion-page.name", "Companion Page"
            ),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.SUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
            },
        )
        self.add_action_holder(self.companion_page_holder)

        self.register()

        log.info(
            f"Companion plugin registered "
            f"(device id {self.get_device_id()}, endpoint {self.get_endpoint_description()})"
        )

        # AppQuit is dispatched synchronously, so this is a real chance to close
        # the socket and join threads rather than relying on daemon threads
        # being killed mid-write.
        try:
            gl.signal_manager.connect_signal(Signals.AppQuit, self.on_app_quit)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Could not register Companion shutdown hook: {exc}")

        self.manager.start()

    # --- Shutdown ----------------------------------------------------------

    def on_app_quit(self, *_args) -> None:
        """Remove our surface from Companion and stop the connection cleanly."""
        log.info("Shutting down Companion connection")
        try:
            self.manager.surface.unregister()
            self.manager.stop()
        except Exception as exc:  # noqa: BLE001
            log.error(f"Error during Companion shutdown: {exc}")

    def on_uninstall(self) -> None:
        self.on_app_quit()

    # --- Connection --------------------------------------------------------

    def _connection_settings(self) -> CompanionConnectionSettings:
        """Build validated connection settings, falling back to defaults.

        A bad host or port must not stop the plugin loading — the settings UI
        is how the user fixes it, and that UI only exists if we got this far.
        """
        try:
            return CompanionConnectionSettings.from_settings(self.get_settings())
        except CompanionConfigError as exc:
            log.error(f"Invalid Companion connection settings, using defaults: {exc}")
            return CompanionConnectionSettings(device_id=self.get_device_id())

    def apply_connection_settings(self) -> None:
        """Reconnect after the user edits host/port/mode."""
        self.manager.update_settings(self._connection_settings())

    def get_settings_area(self):
        """Build the plugin-wide settings page."""
        return self._settings_area.build()

    def apply_debug_logging(self, enabled: bool) -> None:
        """Raise or lower the verbosity of the Companion protocol code.

        Only the plugin's own loggers are touched; StreamController's level is
        left alone.
        """
        logging.getLogger(COMPANION_LOGGER_NAME).setLevel(
            logging.DEBUG if enabled else logging.INFO
        )

    def _on_connection_status(self, status) -> None:
        """Push connection changes to every live action so they redraw."""
        try:
            self._settings_area.update_status(status)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"Could not update Companion settings UI: {exc}")

        for action in self._live_actions():
            try:
                action.on_connection_status_changed(status)
            except Exception as exc:  # noqa: BLE001
                log.error(f"Companion action failed handling status change: {exc}")

    def _live_actions(self) -> list[CompanionButton | CompanionPage]:
        """Every action of ours StreamController currently has instantiated."""
        found: list[CompanionButton | CompanionPage] = []
        try:
            for controller in gl.deck_manager.deck_controller:
                page = controller.active_page
                if page is None:
                    continue
                for action in page.get_all_actions():
                    if isinstance(action, (CompanionButton, CompanionPage)):
                        found.append(action)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"Could not enumerate Companion actions: {exc}")
        return found

    # --- Plugin settings ---------------------------------------------------

    def _ensure_settings_defaults(self) -> None:
        """Fill in missing settings keys and persist them if anything changed.

        Step 18 replaces this with a versioned migration entry point; the
        ``schema_version`` key is written from the start so that migration has
        something to key off.
        """
        settings = self.get_settings() or {}
        original = dict(settings)

        settings.setdefault("schema_version", constants.SCHEMA_VERSION)
        settings.setdefault("connection_mode", constants.DEFAULT_MODE)
        settings.setdefault("satellite_host", constants.DEFAULT_HOST)
        settings.setdefault("satellite_port", constants.DEFAULT_PORT)
        settings.setdefault("debug_logging", False)

        if not settings.get("device_suffix"):
            settings["device_suffix"] = secrets.token_hex(constants.DEVICE_SUFFIX_BYTES)
            log.info(f"Generated Companion device suffix {settings['device_suffix']}")

        if settings != original:
            self.set_settings(settings)

    def get_device_id(self) -> str:
        """The stable identity this installation presents to Companion."""
        suffix = self.get_settings().get("device_suffix", "unknown")
        return f"{constants.DEVICE_ID_PREFIX}-{suffix}"

    def get_endpoint_description(self) -> str:
        settings = self.get_settings()
        host = settings.get("satellite_host", constants.DEFAULT_HOST)
        port = settings.get("satellite_port", constants.DEFAULT_PORT)
        return f"{host}:{port}"
