"""Reroute evaluation: one function decides what the rebook must do.

The planner is the only place that combines in-transit judgement with the
line geometry. The service, the console verdict and the audit detail all
read its output, so they cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.chute.assigner import ChuteAssigner
from bagsort.line.enums import LinePhase, RebookVerdict
from bagsort.line.geometry import LineGeometry, RecallFeasibility
from bagsort.line.targets import TargetBoard
from bagsort.line.tracker import LineTracker
from bagsort.store.store import Store


@dataclass(frozen=True)
class RerouteDecision:
    bag_id: str
    old_flight_id: str
    new_flight_id: str
    phase: LinePhase
    position: int
    verdict: RebookVerdict
    feasibility: RecallFeasibility | None
    new_chute_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "bag_id": self.bag_id,
            "old_flight_id": self.old_flight_id,
            "new_flight_id": self.new_flight_id,
            "phase": self.phase.value,
            "position": self.position,
            "verdict": self.verdict.value,
            "recall": self.feasibility.as_dict() if self.feasibility else None,
            "new_chute_id": self.new_chute_id,
        }


class ReroutePlanner:
    """Pure evaluation over committed state."""

    def __init__(
        self,
        store: Store,
        tracker: LineTracker,
        targets: TargetBoard,
        chutes: ChuteAssigner,
        geometry: LineGeometry,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._targets = targets
        self._chutes = chutes
        self._geometry = geometry

    def evaluate(self, bag_id: str, new_flight_id: str) -> RerouteDecision:
        active = self._targets.require_active(bag_id)
        assignment = self._chutes.require(new_flight_id)
        phase = self._tracker.phase_of(bag_id)
        position = self._tracker.position_of(bag_id)
        verdict, feasibility = self._verdict(phase, position, active.chute_id, assignment.chute_id)
        return RerouteDecision(
            bag_id=bag_id,
            old_flight_id=active.flight_id,
            new_flight_id=new_flight_id,
            phase=phase,
            position=position,
            verdict=verdict,
            feasibility=feasibility,
            new_chute_id=assignment.chute_id,
        )

    def _verdict(
        self,
        phase: LinePhase,
        position: int,
        old_chute_id: str,
        new_chute_id: str,
    ) -> tuple[RebookVerdict, RecallFeasibility | None]:
        if phase is LinePhase.NOT_ENTERED or phase is LinePhase.RECALLED:
            return RebookVerdict.SWAPPED_AT_INDUCTION, None
        if phase is LinePhase.LANDED or phase is LinePhase.EXITED:
            return RebookVerdict.LET_RIDE, None
        feasibility = self._geometry.recall_for(position, old_chute_id)
        if not feasibility.feasible:
            return RebookVerdict.LET_RIDE, feasibility
        # The cancel can land, but re-targeting only makes sense when the new
        # chute is still physically downstream of the bag.
        if not self._geometry.reachable(position, new_chute_id):
            return (
                RebookVerdict.LET_RIDE,
                RecallFeasibility(
                    feasible=False,
                    recall_point=feasibility.recall_point,
                    divert_at=feasibility.divert_at,
                    reason="new_chute_unreachable",
                ),
            )
        return RebookVerdict.RECALLED, feasibility
