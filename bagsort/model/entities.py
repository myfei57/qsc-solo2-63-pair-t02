"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import (
    BagLocation,
    BagState,
    CommandState,
    FlightState,
    RebookAction,
    SortState,
)


def _clean(raw: Any) -> Any:
    """Normalise nested containers so codecs round-trip deterministically."""

    if isinstance(raw, dict):
        return {str(key): _clean(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_clean(item) for item in raw]
    return raw


@dataclass(frozen=True)
class Flight:
    id: str
    code: str
    terminal: str = "T1"
    carousel: str = ""
    chute: str = ""
    state: FlightState = FlightState.OPEN
    batch: int = 0
    schedule_generation: int = 1
    cutoff_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "terminal": self.terminal,
            "carousel": self.carousel,
            "chute": self.chute,
            "state": self.state.value,
            "batch": self.batch,
            "schedule_generation": self.schedule_generation,
            "cutoff_at": self.cutoff_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Flight":
        return cls(
            id=str(raw["id"]),
            code=str(raw.get("code", "")),
            terminal=str(raw.get("terminal", "T1")),
            carousel=str(raw.get("carousel", "")),
            chute=str(raw.get("chute", "")),
            state=FlightState(raw.get("state", FlightState.OPEN.value)),
            batch=int(raw.get("batch", 0)),
            schedule_generation=int(raw.get("schedule_generation", 1)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            closed_at=str(raw.get("closed_at", "")),
        )

    def evolved(self, **changes: Any) -> "Flight":
        return replace(self, **changes)


@dataclass(frozen=True)
class Bag:
    id: str
    barcode: str
    sequence: int
    epoch: int = 0
    flight_id: str = ""
    chute_id: str = ""
    state: BagState = BagState.CHECKED_IN
    location: BagLocation = BagLocation.INDUCTION
    mile: int = 0
    cycle: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "barcode": self.barcode,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
            "location": self.location.value,
            "mile": self.mile,
            "cycle": self.cycle,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Bag":
        return cls(
            id=str(raw["id"]),
            barcode=str(raw.get("barcode", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            state=BagState(raw.get("state", BagState.CHECKED_IN.value)),
            location=BagLocation(raw.get("location", BagLocation.INDUCTION.value)),
            mile=int(raw.get("mile", 0)),
            cycle=int(raw.get("cycle", 0)),
        )

    def evolved(self, **changes: Any) -> "Bag":
        return replace(self, **changes)


@dataclass(frozen=True)
class Reading:
    barcode: str
    bag_id: str
    sequence: int
    epoch: int
    committed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "bag_id": self.bag_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reading":
        return cls(
            barcode=str(raw.get("barcode", "")),
            bag_id=str(raw.get("bag_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            committed=bool(raw.get("committed", False)),
        )


@dataclass(frozen=True)
class ChuteAssignment:
    flight_id: str
    chute_id: str
    generation: int = 1
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChuteAssignment":
        return cls(
            flight_id=str(raw["flight_id"]),
            chute_id=str(raw.get("chute_id", "")),
            generation=int(raw.get("generation", 1)),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass(frozen=True)
class SortRecord:
    bag_id: str
    flight_id: str
    chute_id: str
    sequence: int
    epoch: int
    at: str
    cycle: int = 0
    sort_state: SortState = SortState.LANDED
    reversed_at: str = ""
    reversed_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "at": self.at,
            "cycle": self.cycle,
            "sort_state": self.sort_state.value,
            "reversed_at": self.reversed_at,
            "reversed_reason": self.reversed_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SortRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            at=str(raw.get("at", "")),
            cycle=int(raw.get("cycle", 0)),
            sort_state=SortState(raw.get("sort_state", SortState.LANDED.value)),
            reversed_at=str(raw.get("reversed_at", "")),
            reversed_reason=str(raw.get("reversed_reason", "")),
        )

    def evolved(self, **changes: Any) -> "SortRecord":
        return replace(self, **changes)


@dataclass(frozen=True)
class FlightTarget:
    """The bag to flight mapping as a versioned, single-active row."""

    bag_id: str
    flight_id: str
    version: int = 1
    active: bool = True
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "version": self.version,
            "active": self.active,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FlightTarget":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            version=int(raw.get("version", 1)),
            active=bool(raw.get("active", True)),
            at=str(raw.get("at", "")),
        )

    def evolved(self, **changes: Any) -> "FlightTarget":
        return replace(self, **changes)


@dataclass(frozen=True)
class RebookOrder:
    """One passenger's last-minute change, adjudicated exactly once."""

    id: str
    bag_id: str
    old_flight_id: str
    new_flight_id: str
    at: str
    decided: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bag_id": self.bag_id,
            "old_flight_id": self.old_flight_id,
            "new_flight_id": self.new_flight_id,
            "at": self.at,
            "decided": self.decided,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RebookOrder":
        return cls(
            id=str(raw["id"]),
            bag_id=str(raw.get("bag_id", "")),
            old_flight_id=str(raw.get("old_flight_id", "")),
            new_flight_id=str(raw.get("new_flight_id", "")),
            at=str(raw.get("at", "")),
            decided=bool(raw.get("decided", False)),
        )

    def evolved(self, **changes: Any) -> "RebookOrder":
        return replace(self, **changes)


@dataclass(frozen=True)
class BagDecision:
    """The per-bag conclusion the rebook adjudication committed to the journal."""

    rebook_id: str
    bag_id: str
    old_flight_id: str
    new_flight_id: str
    action: RebookAction
    location: BagLocation
    mile: int
    divert_point: int
    target_chute: str
    feasible: bool
    reason: str
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rebook_id": self.rebook_id,
            "bag_id": self.bag_id,
            "old_flight_id": self.old_flight_id,
            "new_flight_id": self.new_flight_id,
            "action": self.action.value,
            "location": self.location.value,
            "mile": self.mile,
            "divert_point": self.divert_point,
            "target_chute": self.target_chute,
            "feasible": self.feasible,
            "reason": self.reason,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BagDecision":
        return cls(
            rebook_id=str(raw["rebook_id"]),
            bag_id=str(raw.get("bag_id", "")),
            old_flight_id=str(raw.get("old_flight_id", "")),
            new_flight_id=str(raw.get("new_flight_id", "")),
            action=RebookAction(raw.get("action", RebookAction.CONTINUE.value)),
            location=BagLocation(raw.get("location", BagLocation.LINE.value)),
            mile=int(raw.get("mile", 0)),
            divert_point=int(raw.get("divert_point", 0)),
            target_chute=str(raw.get("target_chute", "")),
            feasible=bool(raw.get("feasible", False)),
            reason=str(raw.get("reason", "")),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class LineCommand:
    """An instruction handed to the physical line, idempotent on ``key``."""

    key: str
    bag_id: str
    kind: str
    rebook_id: str = ""
    chute_id: str = ""
    state: CommandState = CommandState.PENDING
    attempts: int = 0
    detail: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "bag_id": self.bag_id,
            "kind": self.kind,
            "rebook_id": self.rebook_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
            "attempts": self.attempts,
            "detail": self.detail,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LineCommand":
        return cls(
            key=str(raw["key"]),
            bag_id=str(raw.get("bag_id", "")),
            kind=str(raw.get("kind", "")),
            rebook_id=str(raw.get("rebook_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            state=CommandState(raw.get("state", CommandState.PENDING.value)),
            attempts=int(raw.get("attempts", 0)),
            detail=str(raw.get("detail", "")),
            at=str(raw.get("at", "")),
        )

    def evolved(self, **changes: Any) -> "LineCommand":
        return replace(self, **changes)


@dataclass(frozen=True)
class RecoveryTask:
    """A bag that kept flying on its old flight and must be pulled on arrival."""

    bag_id: str
    flight_id: str
    new_flight_id: str
    chute_id: str
    rebook_id: str
    at: str
    recovered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "new_flight_id": self.new_flight_id,
            "chute_id": self.chute_id,
            "rebook_id": self.rebook_id,
            "at": self.at,
            "recovered": self.recovered,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RecoveryTask":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            new_flight_id=str(raw.get("new_flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            rebook_id=str(raw.get("rebook_id", "")),
            at=str(raw.get("at", "")),
            recovered=bool(raw.get("recovered", False)),
        )

    def evolved(self, **changes: Any) -> "RecoveryTask":
        return replace(self, **changes)


@dataclass(frozen=True)
class TransferCommand:
    id: str
    flight_id: str
    carousel: str
    batch: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flight_id": self.flight_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TransferCommand":
        return cls(
            id=str(raw["id"]),
            flight_id=str(raw.get("flight_id", "")),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class LoadRecord:
    bag_id: str
    carousel: str
    batch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class Sequence:
    epoch: int = 0
    next_value: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "next": self.next_value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Sequence":
        return cls(epoch=int(raw.get("epoch", 0)), next_value=int(raw.get("next", 0)))


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)
    prev_digest: str = ""
    digest: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "subject": self.subject,
            "detail": _clean(self.detail),
            "prev_digest": self.prev_digest,
            "digest": self.digest,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            kind=str(raw.get("kind", "")),
            subject=str(raw.get("subject", "")),
            detail=_clean(raw.get("detail", {})),
            prev_digest=str(raw.get("prev_digest", "")),
            digest=str(raw.get("digest", "")),
            at=str(raw.get("at", "")),
        )
