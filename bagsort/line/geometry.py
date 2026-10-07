"""Layout of the sortation loop.

Positions are the integer cells a bag passes once it leaves the induction
desk. A flight's chute sits at one cell (its diverter); a recall can only
succeed while the bag is still before that cell, leaving ``margin`` cells for
the physical cancel to land. The geometry is the single place the in-transit
judgement, the reroute decision and the audit detail read their numbers from.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Reserve these cells before the diverter so a cancel still reaches the PLC.
DEFAULT_MARGIN = 2

#: Chutes the seed data uses, with their conventional diverter cells.
DEFAULT_CHUTE_CELLS = {
    "A": 10,
    "B": 20,
    "C": 30,
    "D": 40,
}

DEFAULT_LENGTH = 60


@dataclass(frozen=True)
class RecallFeasibility:
    """Whether one bag can still be pulled back, and the last safe cell."""

    feasible: bool
    recall_point: int
    divert_at: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "feasible": self.feasible,
            "recall_point": self.recall_point,
            "divert_at": self.divert_at,
            "reason": self.reason,
        }


class LineGeometry:
    """Maps chutes to cells and decides recall feasibility."""

    def __init__(self, length: int = DEFAULT_LENGTH, margin: int = DEFAULT_MARGIN) -> None:
        self._length = length
        self._margin = margin
        self._cells = dict(DEFAULT_CHUTE_CELLS)

    @property
    def length(self) -> int:
        return self._length

    @property
    def margin(self) -> int:
        return self._margin

    def set_chute_cell(self, chute_id: str, cell: int) -> None:
        if not (0 < cell <= self._length):
            raise ValueError(f"cell {cell} is outside the sortation loop")
        self._cells[chute_id.strip()[-1:].upper()] = cell

    def chute_cell(self, chute_id: str) -> int:
        key = (chute_id or "").strip()[-1:].upper()
        if key not in self._cells:
            raise KeyError(chute_id)
        return self._cells[key]

    def recall_for(self, position: int, chute_id: str) -> RecallFeasibility:
        """Decide recall against the *old* chute the bag is committed to."""

        divert_at = self.chute_cell(chute_id)
        recall_point = max(0, divert_at - self._margin)
        if position <= recall_point:
            return RecallFeasibility(
                feasible=True,
                recall_point=recall_point,
                divert_at=divert_at,
                reason="before_recall_point",
            )
        if position < divert_at:
            return RecallFeasibility(
                feasible=False,
                recall_point=recall_point,
                divert_at=divert_at,
                reason="past_recall_point",
            )
        return RecallFeasibility(
            feasible=False,
            recall_point=recall_point,
            divert_at=divert_at,
            reason="diverter_committed",
        )

    def reachable(self, position: int, chute_id: str) -> bool:
        """Whether a chute is still downstream of the bag."""

        return self.chute_cell(chute_id) > position
