"""Enumerations for the sortation loop and the rebook flow."""

from __future__ import annotations

from enum import Enum


class LinePhase(str, Enum):
    """Where a bag physically is relative to the sortation loop.

    ``NOT_ENTERED`` is the induction desk / injection belt (导入台): the bag is
    committed but no physical sortation decision has been executed.
    """

    NOT_ENTERED = "not_entered"
    ON_LINE = "on_line"
    LANDED = "landed"
    RECALLED = "recalled"
    EXITED = "exited"


class SortStatus(str, Enum):
    """Lifecycle of one sort record.

    An ``ALLOCATED`` record is a planned divert the loop will execute. It can
    still be rolled back by a recall. A ``LANDED`` record is a physical fact
    and is never deleted; a rollback only ever touches ``ALLOCATED`` rows.
    """

    ALLOCATED = "allocated"
    LANDED = "landed"
    ROLLED_BACK = "rolled_back"


class TargetStatus(str, Enum):
    """Lifecycle of one bag-to-flight target.

    At most one target per bag is ``ACTIVE`` — that row is the single flight
    the plant is allowed to execute. ``PENDING`` rows are decisions waiting on
    physics (a bag still riding to its old chute) and are not effective yet.
    """

    ACTIVE = "active"
    PENDING = "pending"
    SUPERSEDED = "superseded"
    DELIVERED = "delivered"


class DivertState(str, Enum):
    """State of the physical diverter command held by the line gateway."""

    ARMED = "armed"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    FIRED = "fired"


class RebookVerdict(str, Enum):
    """What the reroute decided for one bag."""

    PENDING = "pending"
    SWAPPED_AT_INDUCTION = "swapped_at_induction"
    RECALLED = "recalled"
    LET_RIDE = "let_ride"


class RebookStatus(str, Enum):
    """Whether the rebook order is still staged or has a final verdict."""

    PREPARED = "prepared"
    SETTLED = "settled"
