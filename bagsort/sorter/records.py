"""Read-only projection over the committed sort records.

A sort record is an allocation with a lifecycle. ``planned`` and ``landed``
records are *effective*: they are the instruction the line holds, or the chute
the bag is in. ``reversed`` records stay in the log for audit but no longer
answer as an active allocation. Exactly one effective record exists per bag and
sortation cycle, which is the property the duplicate-dispatch guard relies on.
"""

from __future__ import annotations

from bagsort.model.entities import SortRecord
from bagsort.store.store import Store


class SortLog:
    """Answers the questions dispatch needs before it writes a new record."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def records(self) -> list[SortRecord]:
        return list(self._store.state().sort_records)

    def for_bag(self, bag_id: str) -> list[SortRecord]:
        return [record for record in self._store.state().sort_records if record.bag_id == bag_id]

    def effective_for_bag(self, bag_id: str) -> list[SortRecord]:
        return [record for record in self.for_bag(bag_id) if record.sort_state.effective]

    def has_effective_in_cycle(self, bag_id: str, cycle: int) -> bool:
        return any(
            record.sort_state.effective and record.cycle == cycle
            for record in self.for_bag(bag_id)
        )

    def was_executed(self, bag_id: str) -> bool:
        """Whether the bag still holds an allocation (planned or landed)."""

        return bool(self.effective_for_bag(bag_id))

    def count_for(self, bag_id: str) -> int:
        """All allocations ever issued for the bag, including reversed ones."""

        return len(self.for_bag(bag_id))

    def last_chute(self, bag_id: str) -> str:
        last = ""
        for record in self._store.state().sort_records:
            if record.bag_id == bag_id:
                last = record.chute_id
        return last

    def by_chute(self, flight_id: str | None = None) -> dict[str, int]:
        # Count each bag once, on its current (highest-cycle) effective
        # allocation, so a reversed reroute never double counts the bag.
        current: dict[str, SortRecord] = {}
        for record in self._store.state().sort_records:
            if not record.sort_state.effective:
                continue
            if flight_id is not None and record.flight_id != flight_id:
                continue
            held = current.get(record.bag_id)
            if held is None or record.cycle >= held.cycle:
                current[record.bag_id] = record
        summary: dict[str, int] = {}
        for record in current.values():
            summary[record.chute_id] = summary.get(record.chute_id, 0) + 1
        return dict(sorted(summary.items()))
