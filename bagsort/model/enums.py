"""Enumerations for the two state machines the plant tracks."""

from __future__ import annotations

from enum import Enum


class FlightState(str, Enum):
    """Lifecycle of one flight batch."""

    OPEN = "open"
    BOARDING = "boarding"
    CUTOFF = "cutoff"
    CLOSED = "closed"


class BagState(str, Enum):
    """Lifecycle of one piece of baggage."""

    CHECKED_IN = "checked_in"
    INJECTED = "injected"
    SORTED = "sorted"
    RECHECKED = "rechecked"
    LOADED = "loaded"
    DIVERTED = "diverted"

    @property
    def active(self) -> bool:
        """Whether the bag still occupies plant capacity."""

        return self in (
            BagState.CHECKED_IN,
            BagState.INJECTED,
            BagState.SORTED,
            BagState.RECHECKED,
        )

class Verdict(str, Enum):
    """Result of comparing an observed (epoch, sequence) pair with the high-water mark."""

    FRESH = "fresh"
    REPLAYED = "replayed"
    ROLLED_OVER = "rolled_over"


class BagLocation(str, Enum):
    """Where a bag physically is on the handling line.

    The location is the in-transit adjudication's source of truth: a bag at the
    induction desk has not committed to any lane, a bag on the sortation line has
    a live diversion point ahead of or behind it, and a bag that has landed is
    past every point the control system can still move it at.
    """

    INDUCTION = "induction"
    LINE = "line"
    LANDED = "landed"
    RECOVERY = "recovery"

    @property
    def in_transit(self) -> bool:
        """Whether the control system can still influence the bag's lane."""

        return self is BagLocation.LINE


class SortState(str, Enum):
    """Lifecycle of one sort allocation for one bag.

    ``planned`` allocations are the live instructions the line holds; exactly
    one per bag is effective at a time. ``landed`` marks the bag in a chute.
    ``reversed`` keeps the history but retires the allocation as an instruction,
    which is how a recalled bag's sort record is rolled back without deleting it.
    """

    PLANNED = "planned"
    LANDED = "landed"
    REVERSED = "reversed"

    @property
    def effective(self) -> bool:
        """Whether the allocation is still an instruction the line may act on."""

        return self in (SortState.PLANNED, SortState.LANDED)


class RebookAction(str, Enum):
    """The one decision the adjudication makes for one bag after a re-schedule."""

    RETARGET = "retarget"
    REROUTE = "reroute"
    CONTINUE = "continue"
    RECOVER = "recover"


class CommandState(str, Enum):
    """Lifecycle of a command issued to the physical line controller."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
