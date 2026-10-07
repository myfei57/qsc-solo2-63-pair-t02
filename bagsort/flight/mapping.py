"""Bag to flight mapping, including schedule driven re-mapping.

The mapping has two projections of the same fact:

* ``mappings`` is the fast lookup every router reads;
* ``targets`` is the versioned, single-active history auditors and the console
  reconcile against.

Writing a new target supersedes the previous one in the same transaction, so at
every commit point a bag has at most one active target flight. Remapping a bag
never leaves the old and new flight both valid for it.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import OrderingError
from bagsort.model.entities import FlightTarget
from bagsort.model.errors import unknown_bag
from bagsort.store.store import Store


class BagMapping:
    """Which flight each committed bag currently belongs to."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def assign(self, bag_id: str, flight_id: str) -> None:
        bag = self._store.state().bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        updated = bag.evolved(flight_id=flight_id)
        at = self._clock.now_iso()
        versions = self._store.state().target_versions(bag_id)
        previous = max(versions, key=lambda target: target.version, default=None)
        target = FlightTarget(
            bag_id=bag_id,
            flight_id=flight_id,
            version=(previous.version + 1) if previous else 1,
            active=True,
            at=at,
        )
        with self._store.session() as session:
            session.state.mappings[bag_id] = flight_id
            session.state.bags[bag_id] = updated
            session.record(
                "mapping.put",
                {"bag_id": bag_id, "flight_id": flight_id},
                at,
            )
            # The reducer retires the previous active row to a version key and
            # installs this one as the single active target.
            session.record("target.put", {"target": target.as_dict()}, at)
            session.record("bag.put", {"bag": updated.as_dict()}, at)

    def flight_of(self, bag_id: str) -> str:
        flight_id = self._store.state().mappings.get(bag_id)
        if flight_id is None:
            raise unknown_bag(bag_id)
        return flight_id

    def active_target(self, bag_id: str) -> FlightTarget | None:
        target = self._store.state().targets.get(bag_id)
        return target if target is not None and target.active else None

    def remap(self, bag_id: str, flight_id: str) -> None:
        """Move a bag to the flight the newest schedule names."""

        if self.flight_of(bag_id) == flight_id:
            raise OrderingError(f"bag {bag_id!r} already belongs to {flight_id!r}")
        self.assign(bag_id, flight_id)
