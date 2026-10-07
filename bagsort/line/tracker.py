"""In-transit adjudication: where a bag is and whether a lane change can reach it.

The plant model is one induction desk feeding one ordered sortation line. Every
chute has a *divert point*: the downstream mile marker at which that lane pulls a
bag off the line. A bag still upstream of a divert point can be sent there; a
bag already at or past it has committed to its current lane and no command the
console issues can pull it back.

The tracker only mutates committed journal state, so after a restart it rebuilds
every bag's position from the same log the rest of the plant uses. The
adjudication never trusts in-memory bookkeeping.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, ValidationError
from bagsort.model.entities import Bag
from bagsort.model.enums import BagLocation, BagState
from bagsort.store.store import Store

# Seconds of safety margin between the earliest a reroute command can arrive at
# the divert point and the point itself. A reroute inside the margin is treated
# as too late because the field controller could not guarantee execution.
DEFAULT_INTERCEPT_MARGIN = 4
# Meters a bag advances per simulated second on the line.
DEFAULT_BELT_SPEED = 1
# Fallback divert points for the default seeded lane order (A, B, C, D).
DEFAULT_DIVERT_POINTS = (20, 40, 60, 80)


class LineTracker:
    """Owns bag positions, mileage and the chute divert layout."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        speed: int = DEFAULT_BELT_SPEED,
        intercept_margin: int = DEFAULT_INTERCEPT_MARGIN,
    ) -> None:
        self._store = store
        self._clock = clock
        self._speed = speed
        self._margin = intercept_margin

    # -- divert layout -----------------------------------------------------

    def set_divert_point(self, chute_id: str, mile: int) -> int:
        if mile < 0:
            raise ValidationError("divert point must not be negative")
        with self._store.session() as session:
            session.state.divert_points[chute_id] = mile
            session.record(
                "line.divert_point",
                {"chute_id": chute_id, "mile": mile},
                self._clock.now_iso(),
            )
        return mile

    def divert_point(self, chute_id: str) -> int:
        configured = self._store.state().divert_points.get(chute_id)
        if configured is not None:
            return configured
        tail = chute_id.rsplit("CH", 1)[-1]
        if len(tail) == 1 and tail.isalpha() and tail.isupper():
            index = ord(tail) - ord("A")
            if 0 <= index < len(DEFAULT_DIVERT_POINTS):
                return DEFAULT_DIVERT_POINTS[index]
        raise ConflictError(f"chute {chute_id!r} has no configured divert point")

    def describe_layout(self) -> dict[str, int]:
        return dict(sorted(self._store.state().divert_points.items()))

    # -- bag movement ------------------------------------------------------

    def enter_line(self, bag: Bag) -> Bag:
        """Move a bag off the induction desk and onto the sortation line."""

        if bag.location is not BagLocation.INDUCTION:
            raise ConflictError(f"bag {bag.id!r} is not at the induction desk")
        updated = bag.evolved(location=BagLocation.LINE, mile=0, chute_id="")
        self._put(updated)
        return updated

    def advance(self, bag: Bag, seconds: int = 1) -> Bag:
        if seconds < 0:
            raise ValidationError("elapsed time must not be negative")
        if bag.location is not BagLocation.LINE:
            raise ConflictError(f"bag {bag.id!r} is not on the sortation line")
        updated = bag.evolved(mile=bag.mile + seconds * self._speed)
        self._put(updated)
        return updated

    def land(self, bag: Bag, chute_id: str) -> Bag:
        """Commit a bag into a chute: the point of no return for control."""

        if bag.location is not BagLocation.LINE:
            raise ConflictError(f"bag {bag.id!r} is not still on the line")
        updated = bag.evolved(
            location=BagLocation.LANDED,
            state=BagState.SORTED,
            chute_id=chute_id,
            mile=self.divert_point(chute_id),
        )
        self._put(updated)
        return updated

    # -- adjudication ------------------------------------------------------

    def can_intercept(self, bag: Bag, target_chute: str) -> tuple[bool, str, int]:
        """Decide whether a reroute to ``target_chute`` is still executable.

        Returns ``(feasible, reason, divert_point)``. The position gate is the
        divert point itself; the time gate is the configured safety margin, so a
        bag whose command cannot physically be actioned is never promised a lane.
        """

        divert_point = self.divert_point(target_chute)
        if bag.location is not BagLocation.LINE:
            return False, f"bag is {bag.location.value}, not on the line", divert_point
        remaining = divert_point - bag.mile
        if remaining <= 0:
            return False, "bag has passed the divert point", divert_point
        seconds_to_point = remaining // max(self._speed, 1)
        if seconds_to_point < self._margin:
            return False, "inside the intercept safety margin", divert_point
        return True, "before the divert point with margin to spare", divert_point

    def _put(self, updated: Bag) -> Bag:
        with self._store.session() as session:
            session.state.bags[updated.id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, self._clock.now_iso())
        return updated
