"""Where each bag physically is on the loop, derived from committed state.

Membership of the loop is journaled (``line.put``), so a restart rebuilds
every in-transit position instead of losing it. Movement comes from one of
two places: deterministic ticks (the demo) or scanner sightings
(:meth:`observe`), which is how a real field deployment feeds positions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.model.entities import Bag, LineTracking
from bagsort.model.enums import BagState
from bagsort.sorter.records import SortLog
from bagsort.store.store import Store
from .controller import DivertController, divert_id
from .enums import DivertState, LinePhase
from .gateway import ACK
from .geometry import LineGeometry

if TYPE_CHECKING:
    from .gateway import LineGateway


class LineTracker:
    """Physical phase/position tracking and loop advancement."""

    def __init__(
        self,
        store: Store,
        geometry: LineGeometry,
        controller: DivertController,
        gateway: "LineGateway",
        clock: Clock,
        sort_log: SortLog | None = None,
    ) -> None:
        self._store = store
        self._geometry = geometry
        self._controller = controller
        self._gateway = gateway
        self._clock = clock
        self._sort_log = sort_log or SortLog(store)

    # -- read side --------------------------------------------------------

    def tracking(self, bag_id: str) -> LineTracking | None:
        return self._store.state().line.get(bag_id)

    def phase_of(self, bag_id: str) -> LinePhase:
        tracking = self._store.state().line.get(bag_id)
        if tracking is None:
            return LinePhase.NOT_ENTERED
        return LinePhase(tracking.phase)

    def position_of(self, bag_id: str) -> int:
        tracking = self._store.state().line.get(bag_id)
        return tracking.position if tracking else 0

    def on_loop(self) -> list[LineTracking]:
        return [
            self._store.state().line[key]
            for key in sorted(self._store.state().line)
            if self._store.state().line[key].phase == LinePhase.ON_LINE.value
        ]

    # -- enter / observe --------------------------------------------------

    def enter(self, bag: Bag, chute_id: str, divert_at: int | None = None) -> LineTracking:
        """Put a committed, not-yet-sorted bag onto the loop and arm its divert."""

        from bagsort.line.targets import TargetBoard  # local import keeps wiring light

        if self.phase_of(bag.id) in (LinePhase.ON_LINE, LinePhase.LANDED):
            raise ConflictError(f"bag {bag.id!r} is already on the sortation loop")
        if self._sort_log.effective_count_for(bag.id) > 0:
            raise ConflictError(f"bag {bag.id!r} has already been sorted")
        cell = self._geometry.chute_cell(chute_id) if divert_at is None else divert_at
        at = self._clock.now_iso()
        tracking = LineTracking(
            bag_id=bag.id,
            phase=LinePhase.ON_LINE.value,
            position=0,
            entered_at=at,
            last_seen_at=at,
            last_seen_position=0,
        )
        targets = TargetBoard(self._store, self._clock)
        active = targets.active(bag.id)
        target_number = active.number if active else 0
        command_id = divert_id(bag.id, target_number)
        if active is not None:
            # Prepare: the journal only arms what the physical gateway accepted.
            verdict = self._gateway.post_arm(command_id, bag.id, cell)
            if verdict != ACK:
                raise ConflictError(f"PLC refused to arm the diverter for {bag.id!r}")
        with self._store.session() as session:
            session.state.line[bag.id] = tracking
            session.record("line.put", {"tracking": tracking.as_dict()}, at)
            if active is not None:
                self._controller.issue(
                    session,
                    bag,
                    active.id,
                    target_number,
                    active.flight_id,
                    active.chute_id or chute_id,
                    cell,
                )
        return tracking

    def observe(
        self,
        bag_id: str,
        position: int,
        settle_pending: Callable[[str], None] | None = None,
    ) -> list[str]:
        """Advance the belt until a scanner reports ``bag_id`` at ``position``.

        A real field sighting means the belt physically moved, so everyone
        between sightings moves together; other bags may land on the way.
        """

        tracking = self._require_on_loop(bag_id)
        if position < tracking.position:
            raise ConflictError("a scanner sighting cannot move a bag backwards")
        return self.advance_until(position, settle_pending)

    # -- advancement ------------------------------------------------------

    def tick(
        self,
        cells: int = 1,
        settle_pending: Callable[[str], None] | None = None,
    ) -> list[str]:
        """Move every on-loop bag; land the ones reaching their diverter."""

        landed: list[str] = []
        for tracking in self.on_loop():
            updated = tracking.evolved(position=tracking.position + cells)
            command = self._live_command(updated.bag_id)
            if command is not None and updated.position >= command.divert_at:
                self._land(updated, command)
                landed.append(updated.bag_id)
                if settle_pending is not None:
                    settle_pending(updated.bag_id)
                continue
            at = self._clock.now_iso()
            with self._store.session() as session:
                session.state.line[updated.bag_id] = updated
                session.record("line.put", {"tracking": updated.as_dict()}, at)
        return landed

    def advance_until(
        self,
        position: int,
        settle_pending: Callable[[str], None] | None = None,
    ) -> list[str]:
        """Tick until every bag still on the loop reaches at least ``position``."""

        landed: list[str] = []
        while True:
            furthest = min(
                (tracking.position for tracking in self.on_loop()),
                default=position,
            )
            if furthest >= position:
                return landed
            landed.extend(self.tick(1, settle_pending))

    # -- internals --------------------------------------------------------

    def _live_command(self, bag_id: str):
        for command in self._controller.for_bag(bag_id):
            if command.state == DivertState.ARMED.value:
                return command
        return None

    def _require_on_loop(self, bag_id: str) -> LineTracking:
        tracking = self._store.state().line.get(bag_id)
        if tracking is None or tracking.phase != LinePhase.ON_LINE.value:
            raise ConflictError(f"bag {bag_id!r} is not on the sortation loop")
        return tracking

    def _land(self, tracking: LineTracking, command) -> None:
        at = self._clock.now_iso()
        bag = self._store.state().bags[tracking.bag_id]
        landed = tracking.evolved(
            phase=LinePhase.LANDED.value,
            position=command.divert_at,
            last_seen_at=at,
            last_seen_position=command.divert_at,
        )
        updated_bag = bag.evolved(chute_id=command.chute_id, state=BagState.SORTED)
        with self._store.session() as session:
            session.state.line[tracking.bag_id] = landed
            session.state.bags[tracking.bag_id] = updated_bag
            session.record("line.put", {"tracking": landed.as_dict()}, at)
            session.record("bag.put", {"bag": updated_bag.as_dict()}, at)
            session.state.sort_records = [
                (
                    record.evolved(status="landed", note="landed")
                    if record.bag_id == tracking.bag_id and record.status == "allocated"
                    else record
                )
                for record in session.state.sort_records
            ]
            session.record(
                "sort.land", {"bag_id": tracking.bag_id, "note": "landed", "at": at}, at
            )
            live = session.state.divert_commands.get(command.id)
            if live is not None and live.state == DivertState.ARMED.value:
                self._controller.mark_fired(session, live)
            session.record("counter.add", {"name": "line.landed", "delta": 1}, at)
