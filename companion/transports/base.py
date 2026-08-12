"""Transport-neutral interface.

The action layer must never branch on which transport is in use. Everything
transport-specific — framing, connect semantics, failure
detection — lives behind this interface, so adding the Phase 2 WebSocket
transports cannot require touching ``CompanionButton``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Callable


@dataclass
class TransportCallbacks:
    """Hooks the transport invokes as its connection progresses.

    All of these run on the transport's **worker thread**, never on the caller's
    thread. Implementations must therefore be cheap and thread-aware; anything
    touching StreamController is marshalled further up the stack.

    A transport guarantees it will not invoke any callback after
    :meth:`CompanionTransport.disconnect` has returned.
    """

    on_connected: Callable[[], None]
    on_data: Callable[[bytes], None]
    on_closed: Callable[[str | None], None]
    """Called once per connection attempt that reaches a terminal state. The
    argument is an error description, or ``None`` for a clean close."""


class CompanionTransport(abc.ABC):
    """A byte pipe to Companion with an explicit lifecycle."""

    @abc.abstractmethod
    def connect(self) -> None:
        """Begin connecting. Returns immediately; progress arrives by callback."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Stop and release everything. Safe to call repeatedly and re-entrantly."""

    @abc.abstractmethod
    def send(self, payload: bytes) -> bool:
        """Queue bytes for transmission.

        Never blocks on the network, so it is safe to call from an input
        handler. Returns False if the transport is not in a
        state where the bytes could be queued.
        """

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        """True once the socket is established and not yet closed."""

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Human-readable endpoint, for logs and the settings UI."""
