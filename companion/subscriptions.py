"""Subscription registry, listener fan-out and image cache.

One record per unique Companion address, however many StreamController actions
point at it. Three actions on page 3 / row 2 / column 4 produce **one**
Companion subscription and three local listeners.

Two rules here are easy to get subtly wrong and are load-bearing:

* **Never coalesce image updates.** Every image received is delivered to every
  active listener in arrival order. A press that shows PNG B for 80 ms between
  two PNG A frames must render all three, so the cache records the latest state
  but never suppresses a delivery.
* **Never skip a render because the image is unchanged.** The physical display
  may have changed while a page was inactive.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

from .models import CompanionAddress, SubscriptionEntry

log = logging.getLogger(__name__)

# An image delivery: (address, image). Image is a Pillow image, or None to mean
# "state is no longer known, show a loading/offline visual".
ImageListener = Callable[[CompanionAddress, Any], None]

SubscribeHook = Callable[[CompanionAddress], None]

# How many addresses with no listeners keep their cached image. Bounds memory
# while still letting a page the user flips back to redraw instantly.
_MAX_IDLE_ENTRIES = 256


class SubscriptionRegistry:
    """Tracks listeners per address and fans Companion imagery out to them."""

    def __init__(
        self,
        on_first_listener: SubscribeHook,
        on_last_listener: SubscribeHook,
    ) -> None:
        self._on_first_listener = on_first_listener
        self._on_last_listener = on_last_listener

        self._lock = threading.RLock()
        self._entries: dict[CompanionAddress, SubscriptionEntry] = {}
        # Insertion-ordered record of idle addresses, oldest first.
        self._idle_order: list[CompanionAddress] = []

    # --- Inspection --------------------------------------------------------

    @property
    def addresses(self) -> set[CompanionAddress]:
        with self._lock:
            return set(self._entries)

    @property
    def active_addresses(self) -> set[CompanionAddress]:
        """Addresses with at least one listener — what Companion is subscribed to."""
        with self._lock:
            return {
                address
                for address, entry in self._entries.items()
                if entry.listener_count
            }

    def listener_count(self, address: CompanionAddress) -> int:
        with self._lock:
            entry = self._entries.get(address)
            return entry.listener_count if entry else 0

    def cached_image(self, address: CompanionAddress) -> Any:
        with self._lock:
            entry = self._entries.get(address)
            return entry.cached_image if entry else None

    def entry(self, address: CompanionAddress) -> SubscriptionEntry | None:
        with self._lock:
            return self._entries.get(address)

    # --- Listener lifecycle ------------------------------------------------

    def add_listener(self, address: CompanionAddress, listener: Any) -> Any:
        """Attach a listener and return the cached image, if any.

        The caller renders the returned image immediately rather than waiting
        for Companion to resend it.
        """
        with self._lock:
            entry = self._entries.get(address)
            if entry is None:
                entry = SubscriptionEntry(address=address)
                self._entries[address] = entry

            became_active = entry.add_listener(listener)
            entry.active = True
            self._forget_idle(address)
            cached = entry.cached_image

        if became_active:
            log.debug("Subscribe %s (first listener)", address.describe())
            self._safely(self._on_first_listener, address)
        else:
            log.debug(
                "Subscribe %s (now %d listeners, reusing subscription)",
                address.describe(),
                self.listener_count(address),
            )

        return cached

    def remove_listener(self, address: CompanionAddress, listener: Any) -> None:
        """Detach a listener, dropping the Companion subscription if it was the last."""
        with self._lock:
            entry = self._entries.get(address)
            if entry is None:
                return

            became_idle = entry.remove_listener(listener)
            if became_idle:
                entry.active = False
                self._mark_idle(address)

        if became_idle:
            log.debug("Unsubscribe %s (last listener)", address.describe())
            self._safely(self._on_last_listener, address)

    def remove_listener_everywhere(self, listener: Any) -> None:
        """Detach a listener from every address it is attached to.

        A safety net for action removal: if a settings change raced with
        teardown, this guarantees nothing is left behind in the registry.
        """
        with self._lock:
            addresses = [
                address
                for address, entry in self._entries.items()
                if listener in entry.listeners
            ]

        for address in addresses:
            self.remove_listener(address, listener)

    # --- Image delivery ----------------------------------------------------

    def deliver_image(
        self, address: CompanionAddress, image: Any, generation: int = 0
    ) -> int:
        """Cache an image and hand it to every active listener.

        Returns the number of listeners notified. Delivery is unconditional:
        no equality check against the cached image, and no throttling, because
        either would drop short-lived pressed states.
        """
        with self._lock:
            entry = self._entries.get(address)
            if entry is None:
                # Companion pushed an address nothing is listening to. Cache it
                # so an action appearing later renders instantly.
                entry = SubscriptionEntry(address=address)
                self._entries[address] = entry
                self._mark_idle(address)

            entry.cached_image = image
            entry.last_update = time.monotonic()
            entry.generation = generation
            entry.error = None
            listeners = list(entry.listeners)

        # Fan out with the lock released: listeners render, and holding a lock
        # across UI work invites deadlock.
        for listener in listeners:
            self._deliver_one(listener, address, image)

        return len(listeners)

    def _deliver_one(self, listener: Any, address: CompanionAddress, image: Any) -> None:
        try:
            listener(address, image)
        except Exception:  # noqa: BLE001
            log.error(
                "Image listener raised for %s", address.describe(), exc_info=True
            )

    # --- Invalidation ------------------------------------------------------

    def invalidate(self, addresses: Iterable[CompanionAddress]) -> None:
        """Forget cached imagery and tell listeners the state is unknown.

        Listeners receive ``None``, which they render as a loading/offline
        state rather than continuing to show imagery we can no longer vouch for.
        """
        targets = list(addresses)
        notify: list[tuple[Any, CompanionAddress]] = []

        with self._lock:
            for address in targets:
                entry = self._entries.get(address)
                if entry is None:
                    continue
                entry.invalidate_image()
                notify.extend((listener, address) for listener in entry.listeners)

        for listener, address in notify:
            self._deliver_one(listener, address, None)

    def invalidate_dynamic(self) -> None:
        """Handle Companion's KEYS-CLEAR."""
        with self._lock:
            targets = [a for a in self._entries if a.dynamic_page]
        if targets:
            log.debug("KEYS-CLEAR: invalidating %d dynamic addresses", len(targets))
        self.invalidate(targets)

    def invalidate_all(self) -> None:
        """Distrust every cached image, e.g. after reconnecting.

        Toggle and feedback states may have changed while we were away, so a
        cached PNG is no longer evidence of anything.
        """
        with self._lock:
            targets = list(self._entries)
        self.invalidate(targets)

    def set_error(self, address: CompanionAddress, message: str | None) -> None:
        """Record a per-address error, such as a rejected subscription."""
        with self._lock:
            entry = self._entries.get(address)
            if entry is None:
                return
            entry.error = message
            entry.cached_image = None
            listeners = list(entry.listeners)

        for listener in listeners:
            self._deliver_one(listener, address, None)

    # --- Resubscription ----------------------------------------------------

    def resubscribe_all(self) -> None:
        """Recreate every active Companion subscription after reconnecting."""
        for address in sorted(self.active_addresses, key=lambda a: a.describe()):
            self._safely(self._on_first_listener, address)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._idle_order.clear()

    # --- Idle-entry bookkeeping -------------------------------------------

    def _mark_idle(self, address: CompanionAddress) -> None:
        """Record an entry as listener-less and evict the oldest if over budget."""
        if address in self._idle_order:
            self._idle_order.remove(address)
        self._idle_order.append(address)

        while len(self._idle_order) > _MAX_IDLE_ENTRIES:
            oldest = self._idle_order.pop(0)
            entry = self._entries.get(oldest)
            if entry is not None and not entry.listeners:
                del self._entries[oldest]

    def _forget_idle(self, address: CompanionAddress) -> None:
        if address in self._idle_order:
            self._idle_order.remove(address)

    def _safely(self, hook: SubscribeHook, address: CompanionAddress) -> None:
        try:
            hook(address)
        except Exception:  # noqa: BLE001
            log.error(
                "Subscription hook raised for %s", address.describe(), exc_info=True
            )
