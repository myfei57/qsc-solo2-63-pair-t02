"""Versioned bag-to-flight targets with a single-active guarantee.

A target is a *decision version*. The journaled ``mapping`` projection is
always exactly the set of ``ACTIVE`` targets, so the sorter, the router and
the audit trail can never disagree about which flight one bag currently
owes. ``PENDING`` targets wait on physics (a bag riding to its old chute) and
do not appear in the mapping until they activate.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.model.entities import BagTarget
from bagsort.store.store import Store
from .enums import TargetStatus


def target_id(bag_id: str, number: int) -> str:
    return f"tgt-{bag_id}-{number}"


class TargetBoard:
    """Owns the lifecycle of every bag target inside write transactions."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- read side --------------------------------------------------------

    def for_bag(self, bag_id: str) -> list[BagTarget]:
        return [
            self._store.state().targets[key]
            for key in sorted(self._store.state().targets)
            if key.startswith(f"tgt-{bag_id}-")
        ]

    def active(self, bag_id: str) -> BagTarget | None:
        for target in self.for_bag(bag_id):
            if target.status == TargetStatus.ACTIVE.value:
                return target
        return None

    def pending(self, bag_id: str) -> BagTarget | None:
        for target in self.for_bag(bag_id):
            if target.status == TargetStatus.PENDING.value:
                return target
        return None

    def require_active(self, bag_id: str) -> BagTarget:
        target = self.active(bag_id)
        if target is None:
            raise KeyError(bag_id)
        return target

    # -- write side (call inside a store session) -------------------------

    def create(
        self,
        session,
        bag_id: str,
        flight_id: str,
        chute_id: str,
        status: TargetStatus,
        request_id: str = "",
        at: str = "",
    ) -> BagTarget:
        """Append a new target version; ids are deterministic on replay."""

        number = len(self.for_bag(bag_id)) + 1
        target = BagTarget(
            id=target_id(bag_id, number),
            bag_id=bag_id,
            number=number,
            flight_id=flight_id,
            chute_id=chute_id,
            status=status.value,
            created_at=at or self._clock.now_iso(),
            request_id=request_id,
        )
        session.state.targets[target.id] = target
        session.record("target.put", {"target": target.as_dict()}, target.created_at)
        return target

    def settle(self, session, target: BagTarget, status: TargetStatus) -> BagTarget:
        updated = target.evolved(status=status.value)
        session.state.targets[updated.id] = updated
        session.record("target.put", {"target": updated.as_dict()}, self._clock.now_iso())
        return updated

    def activate(self, session, bag_id: str, target: BagTarget) -> BagTarget:
        """Supersede the current active target and make ``target`` effective.

        The mapping row is written in the same transaction, so the
        single-active-target rule and the bag/flight mapping can never diverge.
        """

        current = self.active(bag_id)
        if current is not None and current.id != target.id:
            self.settle(session, current, TargetStatus.SUPERSEDED)
        if target.status != TargetStatus.ACTIVE.value:
            target = self.settle(session, target, TargetStatus.ACTIVE)
        session.state.mappings[bag_id] = target.flight_id
        session.record(
            "mapping.put",
            {"bag_id": bag_id, "flight_id": target.flight_id},
            self._clock.now_iso(),
        )
        return target

    def deactivate(self, session, bag_id: str, status: TargetStatus) -> None:
        """Retire the active target and drop its mapping row together."""

        current = self.active(bag_id)
        if current is not None:
            self.settle(session, current, status)
        session.state.mappings.pop(bag_id, None)
        session.record("mapping.remove", {"bag_id": bag_id}, self._clock.now_iso())
