"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import BagState, FlightState


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "barcode": self.barcode,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
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
    status: str = "landed"
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "at": self.at,
            "status": self.status,
            "note": self.note,
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
            status=str(raw.get("status", "landed")),
            note=str(raw.get("note", "")),
        )

    @property
    def effective(self) -> bool:
        """A landed record that has not been rolled back."""

        return self.status != "rolled_back"

    def evolved(self, **changes: Any) -> "SortRecord":
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


@dataclass(frozen=True)
class LineTracking:
    """Physical position of one bag on the sortation loop."""

    bag_id: str
    phase: str
    position: int
    entered_at: str
    last_seen_at: str
    last_seen_position: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "phase": self.phase,
            "position": self.position,
            "entered_at": self.entered_at,
            "last_seen_at": self.last_seen_at,
            "last_seen_position": self.last_seen_position,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LineTracking":
        return cls(
            bag_id=str(raw["bag_id"]),
            phase=str(raw.get("phase", "not_entered")),
            position=int(raw.get("position", 0)),
            entered_at=str(raw.get("entered_at", "")),
            last_seen_at=str(raw.get("last_seen_at", "")),
            last_seen_position=int(raw.get("last_seen_position", 0)),
        )

    def evolved(self, **changes: Any) -> "LineTracking":
        return replace(self, **changes)


@dataclass(frozen=True)
class DivertCommand:
    """The one physical diverter arming that realises one bag target."""

    id: str
    bag_id: str
    target_id: str
    flight_id: str
    chute_id: str
    divert_at: int
    state: str
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bag_id": self.bag_id,
            "target_id": self.target_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "divert_at": self.divert_at,
            "state": self.state,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DivertCommand":
        return cls(
            id=str(raw["id"]),
            bag_id=str(raw["bag_id"]),
            target_id=str(raw.get("target_id", "")),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            divert_at=int(raw.get("divert_at", 0)),
            state=str(raw.get("state", "armed")),
            reason=str(raw.get("reason", "")),
        )

    def evolved(self, **changes: Any) -> "DivertCommand":
        return replace(self, **changes)


@dataclass(frozen=True)
class BagTarget:
    """One version of a bag's flight target.

    Each target carries the id of the rebook order that created it (empty for
    the original check-in target), which lets the audit trail join the policy
    decision to exactly one mapping change.
    """

    id: str
    bag_id: str
    number: int
    flight_id: str
    chute_id: str
    status: str
    created_at: str
    request_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bag_id": self.bag_id,
            "number": self.number,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "status": self.status,
            "created_at": self.created_at,
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BagTarget":
        return cls(
            id=str(raw["id"]),
            bag_id=str(raw["bag_id"]),
            number=int(raw.get("number", 0)),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            status=str(raw.get("status", "active")),
            created_at=str(raw.get("created_at", "")),
            request_id=str(raw.get("request_id", "")),
        )

    def evolved(self, **changes: Any) -> "BagTarget":
        return replace(self, **changes)


@dataclass(frozen=True)
class RebookOrder:
    """The persisted conclusion of one passenger rebook for one bag."""

    id: str
    request_id: str
    bag_id: str
    old_flight_id: str
    new_flight_id: str
    status: str
    verdict: str
    phase: str
    position: int
    recall_point: int
    divert_at: int
    target_id: str
    command_id: str
    landed_chute_id: str
    reason: str
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "bag_id": self.bag_id,
            "old_flight_id": self.old_flight_id,
            "new_flight_id": self.new_flight_id,
            "status": self.status,
            "verdict": self.verdict,
            "phase": self.phase,
            "position": self.position,
            "recall_point": self.recall_point,
            "divert_at": self.divert_at,
            "target_id": self.target_id,
            "command_id": self.command_id,
            "landed_chute_id": self.landed_chute_id,
            "reason": self.reason,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RebookOrder":
        return cls(
            id=str(raw["id"]),
            request_id=str(raw.get("request_id", "")),
            bag_id=str(raw["bag_id"]),
            old_flight_id=str(raw.get("old_flight_id", "")),
            new_flight_id=str(raw.get("new_flight_id", "")),
            status=str(raw.get("status", "settled")),
            verdict=str(raw.get("verdict", "")),
            phase=str(raw.get("phase", "")),
            position=int(raw.get("position", 0)),
            recall_point=int(raw.get("recall_point", 0)),
            divert_at=int(raw.get("divert_at", 0)),
            target_id=str(raw.get("target_id", "")),
            command_id=str(raw.get("command_id", "")),
            landed_chute_id=str(raw.get("landed_chute_id", "")),
            reason=str(raw.get("reason", "")),
            at=str(raw.get("at", "")),
        )

    def evolved(self, **changes: Any) -> "RebookOrder":
        return replace(self, **changes)


@dataclass(frozen=True)
class RebookPolicy:
    """The sealed line-wide orientation chosen for rebooks."""

    name: str
    rejected: str
    rationale: str
    sealed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rejected": self.rejected,
            "rationale": self.rationale,
            "sealed_at": self.sealed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RebookPolicy":
        return cls(
            name=str(raw.get("name", "")),
            rejected=str(raw.get("rejected", "")),
            rationale=str(raw.get("rationale", "")),
            sealed_at=str(raw.get("sealed_at", "")),
        )
