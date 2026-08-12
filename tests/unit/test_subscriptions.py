"""Subscription registry tests."""

from __future__ import annotations

from companion.models import CompanionAddress
from companion.subscriptions import SubscriptionRegistry


class Hooks:
    """Records which addresses caused network subscribe/unsubscribe calls."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    def on_first(self, address: CompanionAddress) -> None:
        self.subscribed.append(address.describe())

    def on_last(self, address: CompanionAddress) -> None:
        self.unsubscribed.append(address.describe())


class Listener:
    """A stand-in for an action; records everything rendered to it."""

    def __init__(self, name: str = "listener") -> None:
        self.name = name
        self.renders: list[object] = []

    def __call__(self, address: CompanionAddress, image: object) -> None:
        self.renders.append(image)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Listener {self.name}>"


def _registry() -> tuple[SubscriptionRegistry, Hooks]:
    hooks = Hooks()
    return SubscriptionRegistry(hooks.on_first, hooks.on_last), hooks


ADDRESS = CompanionAddress.from_ui(dynamic_page=False, page=3, row=1, column=3)
OTHER = CompanionAddress.from_ui(dynamic_page=False, page=1, row=0, column=0)
DYNAMIC = CompanionAddress.from_ui(dynamic_page=True, row=0, column=0)


# --- Deduplication ---------------------------------------------------------


class TestDeduplication:
    def test_three_listeners_create_one_subscription(self):
        """Three listeners on one address must produce exactly one subscription."""
        registry, hooks = _registry()

        for name in ("a", "b", "c"):
            registry.add_listener(ADDRESS, Listener(name))

        assert hooks.subscribed == ["static/3/1/3"]
        assert registry.listener_count(ADDRESS) == 3

    def test_listener_lifecycle_matches_the_reference_table(self):
        """Only 0->1 and 1->0 touch the network."""
        registry, hooks = _registry()
        a, b, c = Listener("a"), Listener("b"), Listener("c")

        registry.add_listener(ADDRESS, a)
        assert hooks.subscribed == ["static/3/1/3"]

        registry.add_listener(ADDRESS, b)
        registry.add_listener(ADDRESS, c)
        assert hooks.subscribed == ["static/3/1/3"], "no extra subscriptions"

        registry.remove_listener(ADDRESS, b)
        assert hooks.unsubscribed == []

        registry.remove_listener(ADDRESS, a)
        assert hooks.unsubscribed == []

        registry.remove_listener(ADDRESS, c)
        assert hooks.unsubscribed == ["static/3/1/3"]

    def test_different_addresses_subscribe_separately(self):
        registry, hooks = _registry()

        registry.add_listener(ADDRESS, Listener())
        registry.add_listener(OTHER, Listener())

        assert sorted(hooks.subscribed) == ["static/1/0/0", "static/3/1/3"]

    def test_dynamic_and_static_at_the_same_coordinates_are_distinct(self):
        registry, hooks = _registry()
        static = CompanionAddress.from_ui(dynamic_page=False, page=1, row=0, column=0)

        registry.add_listener(DYNAMIC, Listener())
        registry.add_listener(static, Listener())

        assert len(hooks.subscribed) == 2

    def test_re_adding_the_same_listener_does_not_double_count(self):
        registry, hooks = _registry()
        listener = Listener()

        registry.add_listener(ADDRESS, listener)
        registry.add_listener(ADDRESS, listener)

        assert registry.listener_count(ADDRESS) == 1
        assert hooks.subscribed == ["static/3/1/3"]

        registry.remove_listener(ADDRESS, listener)
        assert hooks.unsubscribed == ["static/3/1/3"]

    def test_removing_an_unknown_address_is_harmless(self):
        registry, hooks = _registry()

        registry.remove_listener(ADDRESS, Listener())

        assert hooks.unsubscribed == []


# --- Image delivery --------------------------------------------------------


class TestImageDelivery:
    def test_image_reaches_every_listener(self):
        registry, _ = _registry()
        a, b = Listener("a"), Listener("b")
        registry.add_listener(ADDRESS, a)
        registry.add_listener(ADDRESS, b)

        registry.deliver_image(ADDRESS, "IMG")

        assert a.renders == ["IMG"]
        assert b.renders == ["IMG"]

    def test_a_press_release_sequence_is_not_collapsed(self):
        """A -> B -> A must render all three, in order."""
        registry, _ = _registry()
        listener = Listener()
        registry.add_listener(ADDRESS, listener)

        registry.deliver_image(ADDRESS, "PNG_A")
        registry.deliver_image(ADDRESS, "PNG_B")
        registry.deliver_image(ADDRESS, "PNG_A")

        assert listener.renders == ["PNG_A", "PNG_B", "PNG_A"]

    def test_an_identical_image_is_still_delivered(self):
        """The physical display may have changed underneath us."""
        registry, _ = _registry()
        listener = Listener()
        registry.add_listener(ADDRESS, listener)

        registry.deliver_image(ADDRESS, "SAME")
        registry.deliver_image(ADDRESS, "SAME")
        registry.deliver_image(ADDRESS, "SAME")

        assert listener.renders == ["SAME", "SAME", "SAME"]

    def test_rapid_updates_all_arrive_in_order(self):
        registry, _ = _registry()
        listener = Listener()
        registry.add_listener(ADDRESS, listener)

        for index in range(100):
            registry.deliver_image(ADDRESS, index)

        assert listener.renders == list(range(100))

    def test_delivery_to_an_unwatched_address_is_cached(self):
        """An action appearing later renders instantly instead of waiting."""
        registry, _ = _registry()

        registry.deliver_image(ADDRESS, "EARLY")
        listener = Listener()
        cached = registry.add_listener(ADDRESS, listener)

        assert cached == "EARLY"

    def test_a_raising_listener_does_not_block_the_others(self):
        registry, _ = _registry()
        good = Listener("good")

        def explode(address, image):
            raise RuntimeError("render failed")

        registry.add_listener(ADDRESS, explode)
        registry.add_listener(ADDRESS, good)

        registry.deliver_image(ADDRESS, "IMG")

        assert good.renders == ["IMG"]

    def test_removed_listeners_stop_receiving(self):
        registry, _ = _registry()
        listener = Listener()
        registry.add_listener(ADDRESS, listener)
        registry.remove_listener(ADDRESS, listener)

        registry.deliver_image(ADDRESS, "IMG")

        assert listener.renders == []


# --- Cache -----------------------------------------------------------------


class TestCache:
    def test_first_image_is_stored(self):
        registry, _ = _registry()
        registry.add_listener(ADDRESS, Listener())

        registry.deliver_image(ADDRESS, "FIRST")

        assert registry.cached_image(ADDRESS) == "FIRST"

    def test_update_replaces_the_previous_image(self):
        registry, _ = _registry()
        registry.add_listener(ADDRESS, Listener())

        registry.deliver_image(ADDRESS, "FIRST")
        registry.deliver_image(ADDRESS, "SECOND")

        assert registry.cached_image(ADDRESS) == "SECOND"

    def test_a_new_listener_receives_the_cached_image(self):
        registry, _ = _registry()
        registry.add_listener(ADDRESS, Listener("a"))
        registry.deliver_image(ADDRESS, "CACHED")

        cached = registry.add_listener(ADDRESS, Listener("b"))

        assert cached == "CACHED"

    def test_the_first_listener_gets_no_cached_image(self):
        registry, _ = _registry()

        assert registry.add_listener(ADDRESS, Listener()) is None

    def test_cache_survives_the_last_listener_leaving(self):
        """So flipping back to a page can redraw immediately."""
        registry, _ = _registry()
        listener = Listener()
        registry.add_listener(ADDRESS, listener)
        registry.deliver_image(ADDRESS, "KEEP")

        registry.remove_listener(ADDRESS, listener)

        assert registry.cached_image(ADDRESS) == "KEEP"

    def test_idle_entries_are_bounded(self):
        """Page churn must not grow the cache without limit."""
        registry, _ = _registry()

        for index in range(1, 400):
            address = CompanionAddress.from_ui(
                dynamic_page=False, page=index, row=0, column=0
            )
            listener = Listener()
            registry.add_listener(address, listener)
            registry.deliver_image(address, index)
            registry.remove_listener(address, listener)

        assert len(registry.addresses) <= 256


# --- Invalidation ----------------------------------------------------------


class TestInvalidation:
    def test_keys_clear_invalidates_dynamic_only(self):
        """KEYS-CLEAR must invalidate every dynamic address."""
        registry, _ = _registry()
        dynamic_listener, static_listener = Listener("dyn"), Listener("static")
        registry.add_listener(DYNAMIC, dynamic_listener)
        registry.add_listener(ADDRESS, static_listener)
        registry.deliver_image(DYNAMIC, "DYN")
        registry.deliver_image(ADDRESS, "STATIC")

        registry.invalidate_dynamic()

        assert registry.cached_image(DYNAMIC) is None
        assert registry.cached_image(ADDRESS) == "STATIC"

    def test_keys_clear_tells_listeners_the_state_is_unknown(self):
        registry, _ = _registry()
        listener = Listener()
        registry.add_listener(DYNAMIC, listener)
        registry.deliver_image(DYNAMIC, "DYN")

        registry.invalidate_dynamic()

        assert listener.renders == ["DYN", None]

    def test_invalidate_all_distrusts_every_cached_image(self):
        """After reconnect, a cached PNG proves nothing."""
        registry, _ = _registry()
        registry.add_listener(DYNAMIC, Listener())
        registry.add_listener(ADDRESS, Listener())
        registry.deliver_image(DYNAMIC, "A")
        registry.deliver_image(ADDRESS, "B")

        registry.invalidate_all()

        assert registry.cached_image(DYNAMIC) is None
        assert registry.cached_image(ADDRESS) is None

    def test_invalidation_keeps_listeners_attached(self):
        registry, hooks = _registry()
        registry.add_listener(ADDRESS, Listener())

        registry.invalidate_all()

        assert registry.listener_count(ADDRESS) == 1
        assert hooks.unsubscribed == []

    def test_set_error_clears_the_image_and_notifies(self):
        registry, _ = _registry()
        listener = Listener()
        registry.add_listener(ADDRESS, listener)
        registry.deliver_image(ADDRESS, "IMG")

        registry.set_error(ADDRESS, "subscriptions unsupported")

        assert registry.cached_image(ADDRESS) is None
        assert listener.renders == ["IMG", None]
        assert registry.entry(ADDRESS).error == "subscriptions unsupported"


# --- Resubscription --------------------------------------------------------


class TestResubscription:
    def test_resubscribe_covers_every_active_address(self):
        registry, hooks = _registry()
        registry.add_listener(ADDRESS, Listener())
        registry.add_listener(OTHER, Listener())
        hooks.subscribed.clear()

        registry.resubscribe_all()

        assert sorted(hooks.subscribed) == ["static/1/0/0", "static/3/1/3"]

    def test_resubscribe_skips_addresses_with_no_listeners(self):
        registry, hooks = _registry()
        listener = Listener()
        registry.add_listener(ADDRESS, listener)
        registry.remove_listener(ADDRESS, listener)
        hooks.subscribed.clear()

        registry.resubscribe_all()

        assert hooks.subscribed == []

    def test_resubscribe_emits_one_call_per_address_not_per_listener(self):
        registry, hooks = _registry()
        for name in ("a", "b", "c"):
            registry.add_listener(ADDRESS, Listener(name))
        hooks.subscribed.clear()

        registry.resubscribe_all()

        assert hooks.subscribed == ["static/3/1/3"]


# --- Teardown --------------------------------------------------------------


class TestTeardown:
    def test_remove_listener_everywhere_finds_every_address(self):
        """No deleted action may linger in the maps."""
        registry, hooks = _registry()
        listener = Listener()
        registry.add_listener(ADDRESS, listener)
        registry.add_listener(OTHER, listener)
        registry.add_listener(DYNAMIC, listener)

        registry.remove_listener_everywhere(listener)

        assert registry.active_addresses == set()
        assert len(hooks.unsubscribed) == 3

    def test_remove_everywhere_leaves_other_listeners_alone(self):
        registry, _ = _registry()
        leaving, staying = Listener("leaving"), Listener("staying")
        registry.add_listener(ADDRESS, leaving)
        registry.add_listener(ADDRESS, staying)

        registry.remove_listener_everywhere(leaving)

        assert registry.listener_count(ADDRESS) == 1

    def test_no_listener_leak_across_repeated_page_churn(self):
        """Watch specifically for subscription leaks."""
        registry, hooks = _registry()

        for _ in range(200):
            listener = Listener()
            registry.add_listener(ADDRESS, listener)
            registry.remove_listener(ADDRESS, listener)

        assert registry.listener_count(ADDRESS) == 0
        assert registry.active_addresses == set()
        assert len(hooks.subscribed) == len(hooks.unsubscribed) == 200
