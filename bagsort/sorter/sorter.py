"""Dispatch: the step that turns a committed reading into a sort record.

Dispatch refuses an uncommitted reading outright, and it refuses a bag that
already holds an effective allocation. Both refusals are the contracts other
subsystems rely on: the desk must commit before routing, and a retried induction
must not create a second live sort record.

An allocation moves through a lifecycle:

* ``plan`` writes a ``planned`` record while the bag is still on the line, so a
  rebook adjudication may still ``reverse`` it and reroute;
* ``land`` confirms the bag into the chute and flips the record to ``landed``;
* ``dispatch`` is the legacy one-shot plan-and-land used by manual induction.

Every write for one bag goes through :meth:`Sorter._open_plan`, which is the
single choke point the duplicate guard and the cycle bookkeeping live in.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.model.entities import Bag, Reading, SortRecord
from bagsort.model.enums import BagLocation, BagState, SortState
from bagsort.sequence.verdict import VerdictTracker
from bagsort.store.store import Session, Store
from bagsort.chute.assigner import ChuteAssigner
from bagsort.flight.mapping import BagMapping
from .records import SortLog
from .route import RouteDetail


class Sorter:
    """Routes committed bags to their flight's current chute."""

    def __init__(
        self,
        store: Store,
        mapping: BagMapping,
        chutes: ChuteAssigner,
        clock: Clock,
        verdict: VerdictTracker | None = None,
    ) -> None:
        self._store = store
        self._mapping = mapping
        self._chutes = chutes
        self._clock = clock
        self._verdict = verdict or VerdictTracker()
        self._log = SortLog(store)

    @property
    def log(self) -> SortLog:
        return self._log

    def route(self, bag_id: str) -> RouteDetail:
        flight_id = self._mapping.flight_of(bag_id)
        return self.route_for(bag_id, flight_id)

    def route_for(self, bag_id: str, flight_id: str) -> RouteDetail:
        """Resolve a bag's route against an explicit flight (used by re-routes)."""

        assignment = self._chutes.require(flight_id)
        return RouteDetail(
            bag_id=bag_id,
            flight_id=flight_id,
            chute_id=assignment.chute_id,
            generation=assignment.generation,
        )

    def dispatch(self, bag: Bag, reading: Reading) -> SortRecord:
        """One-shot plan and land: assign the lane and commit the chute."""

        detail, at = self._guard(bag, reading)
        record = self._record(detail, bag, at, SortState.LANDED)
        updated = bag.evolved(
            chute_id=detail.chute_id,
            state=BagState.SORTED,
            location=BagLocation.LANDED,
        )
        with self._store.session() as session:
            session.state.sort_records.append(record)
            session.state.bags[updated.id] = updated
            session.record("sort.record", {"record": record.as_dict()}, at)
            session.record("bag.put", {"bag": updated.as_dict()}, at)
            session.record("counter.add", {"name": "sorter.dispatched", "delta": 1}, at)
        return record

    def plan(self, bag: Bag, reading: Reading) -> SortRecord:
        """Issue a planned allocation for a bag that is still on the line."""

        detail, at = self._guard(bag, reading)
        record = self._record(detail, bag, at, SortState.PLANNED)
        with self._store.session() as session:
            session.state.sort_records.append(record)
            session.record("sort.plan", {"record": record.as_dict()}, at)
            session.record("counter.add", {"name": "sorter.planned", "delta": 1}, at)
        return record

    def land(self, bag: Bag, mile: int | None = None) -> SortRecord:
        """Confirm the planned allocation has put the bag in its chute."""

        planned = self._open_record(bag.id, bag.cycle)
        if planned is None or planned.sort_state is not SortState.PLANNED:
            raise ConflictError(f"bag {bag.id!r} has no planned allocation to land")
        at = self._clock.now_iso()
        landed = planned.evolved(sort_state=SortState.LANDED)
        updated = bag.evolved(
            chute_id=landed.chute_id,
            state=BagState.SORTED,
            location=BagLocation.LANDED,
            mile=bag.mile if mile is None else mile,
        )
        with self._store.session() as session:
            self._replace(session, planned, landed)
            session.state.bags[updated.id] = updated
            session.record(
                "sort.land", {"bag_id": bag.id, "cycle": bag.cycle, "at": at}, at
            )
            session.record("bag.put", {"bag": updated.as_dict()}, at)
            session.record("counter.add", {"name": "sorter.landed", "delta": 1}, at)
        return landed

    def reverse(self, bag: Bag, reason: str) -> SortRecord | None:
        """Roll back the bag's current effective allocation as an instruction."""

        at = self._clock.now_iso()
        with self._store.session() as session:
            reversed_record = self.reverse_in(session, bag, reason, at)
            if reversed_record is None:
                return None
        return reversed_record

    # -- session-scoped primitives, used by the rebook adjudication so the
    # target swap and the allocation rollback commit as one transaction.

    def reverse_in(
        self, session: Session, bag: Bag, reason: str, at: str
    ) -> SortRecord | None:
        effective = self._open_record_in(session, bag.id, bag.cycle)
        if effective is None or not effective.sort_state.effective:
            return None
        reversed_record = effective.evolved(
            sort_state=SortState.REVERSED,
            reversed_at=at,
            reversed_reason=reason,
        )
        self._replace(session, effective, reversed_record)
        session.record(
            "sort.reverse",
            {
                "bag_id": bag.id,
                "cycle": bag.cycle,
                "reason": reason,
                "chute_id": effective.chute_id,
                "at": at,
            },
            at,
        )
        session.record("counter.add", {"name": "sorter.reversed", "delta": 1}, at)
        return reversed_record

    def plan_in(
        self,
        session: Session,
        bag: Bag,
        detail: RouteDetail,
        at: str,
    ) -> SortRecord:
        record = self._record(detail, bag, at, SortState.PLANNED)
        session.state.sort_records.append(record)
        session.record("sort.plan", {"record": record.as_dict()}, at)
        session.record("counter.add", {"name": "sorter.planned", "delta": 1}, at)
        return record

    def divert(self, bag: Bag) -> Bag:
        updated = bag.evolved(state=BagState.DIVERTED)
        with self._store.session() as session:
            session.state.bags[updated.id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, self._clock.now_iso())
            session.record(
                "counter.add", {"name": "sorter.diverted", "delta": 1}, self._clock.now_iso()
            )
        return updated

    def verdict(self, epoch: int, sequence: int):
        """Classify an observed pair and advance the mark when it is fresh."""

        return self._verdict.observe(epoch, sequence)

    def is_fresh(self, epoch: int, sequence: int) -> bool:
        return self._verdict.is_fresh(epoch, sequence)

    def high_water(self) -> tuple[int, int]:
        return self._verdict.high_water()

    def was_executed(self, bag_id: str) -> bool:
        return self._log.was_executed(bag_id)

    def records(self) -> list[SortRecord]:
        return self._log.records()

    def assign_chute(self, flight_id: str, chute_id: str):
        """Convenience for the injection belt, which sets the lane then dispatches."""

        return self._chutes.assign(flight_id, chute_id)

    # -- internals ---------------------------------------------------------

    def _guard(self, bag: Bag, reading: Reading) -> tuple[RouteDetail, str]:
        if not reading.committed:
            raise ConflictError(f"barcode read for {bag.barcode!r} is not committed")
        if self._log.has_effective_in_cycle(bag.id, bag.cycle):
            raise ConflictError(
                f"bag {bag.id!r} has already been sorted in cycle {bag.cycle}"
            )
        return self.route(bag.id), self._clock.now_iso()

    def _record(
        self,
        detail: RouteDetail,
        bag: Bag,
        at: str,
        sort_state: SortState,
    ) -> SortRecord:
        return SortRecord(
            bag_id=bag.id,
            flight_id=detail.flight_id,
            chute_id=detail.chute_id,
            sequence=bag.sequence,
            epoch=bag.epoch,
            at=at,
            cycle=bag.cycle,
            sort_state=sort_state,
        )

    def _open_record(self, bag_id: str, cycle: int) -> SortRecord | None:
        for record in self._store.state().sort_records:
            if record.bag_id == bag_id and record.cycle == cycle and record.sort_state.effective:
                return record
        return None

    def _open_record_in(
        self, session: Session, bag_id: str, cycle: int
    ) -> SortRecord | None:
        for record in session.state.sort_records:
            if record.bag_id == bag_id and record.cycle == cycle and record.sort_state.effective:
                return record
        return None

    def _replace(
        self, session: Session, previous: SortRecord, current: SortRecord
    ) -> None:
        session.state.sort_records = [
            current if record is previous else record for record in session.state.sort_records
        ]
