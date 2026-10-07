"""The folded document every subsystem reads and the reducer that builds it.

The store keeps an append-only journal of committed commands. This module turns
that journal into the materialised state and knows how to apply one command,
which is what makes restart-by-replay possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .entities import (
    AuditEntry,
    Bag,
    BagDecision,
    ChuteAssignment,
    Flight,
    FlightTarget,
    LineCommand,
    LoadRecord,
    RebookOrder,
    RecoveryTask,
    Sequence,
    SortRecord,
    TransferCommand,
)
from bagsort.errors import ValidationError
from .enums import SortState


@dataclass
class State:
    """Materialised view of every committed command."""

    flights: dict[str, Flight] = field(default_factory=dict)
    bags: dict[str, Bag] = field(default_factory=dict)
    mappings: dict[str, str] = field(default_factory=dict)
    targets: dict[str, FlightTarget] = field(default_factory=dict)
    assignments: dict[str, ChuteAssignment] = field(default_factory=dict)
    sort_records: list[SortRecord] = field(default_factory=list)
    rebook_orders: dict[str, RebookOrder] = field(default_factory=dict)
    decisions: list[BagDecision] = field(default_factory=list)
    line_commands: dict[str, LineCommand] = field(default_factory=dict)
    recovery_tasks: dict[str, RecoveryTask] = field(default_factory=dict)
    divert_points: dict[str, int] = field(default_factory=dict)
    transfer_commands: list[TransferCommand] = field(default_factory=list)
    load_records: list[LoadRecord] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    quota_limits: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    sequence: Sequence = field(default_factory=Sequence)
    recheck_queue: list[str] = field(default_factory=list)
    belt_carousel: str = ""
    belt_batch: int = 0
    belt_on_carousel: list[str] = field(default_factory=list)

    def copy(self) -> "State":
        return State(
            flights=dict(self.flights),
            bags=dict(self.bags),
            mappings=dict(self.mappings),
            targets=dict(self.targets),
            assignments=dict(self.assignments),
            sort_records=list(self.sort_records),
            rebook_orders=dict(self.rebook_orders),
            decisions=list(self.decisions),
            line_commands=dict(self.line_commands),
            recovery_tasks=dict(self.recovery_tasks),
            divert_points=dict(self.divert_points),
            transfer_commands=list(self.transfer_commands),
            load_records=list(self.load_records),
            audit=list(self.audit),
            quota_limits=dict(self.quota_limits),
            counters=dict(self.counters),
            sequence=self.sequence,
            recheck_queue=list(self.recheck_queue),
            belt_carousel=self.belt_carousel,
            belt_batch=self.belt_batch,
            belt_on_carousel=list(self.belt_on_carousel),
        )

    def active_target(self, bag_id: str) -> FlightTarget | None:
        target = self.targets.get(bag_id)
        return target if target is not None and target.active else None

    def target_versions(self, bag_id: str) -> list[FlightTarget]:
        return [
            target
            for target in self.targets.values()
            if target.bag_id == bag_id
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "flights": {key: value.as_dict() for key, value in sorted(self.flights.items())},
            "bags": {key: value.as_dict() for key, value in sorted(self.bags.items())},
            "mappings": dict(sorted(self.mappings.items())),
            "targets": {key: value.as_dict() for key, value in sorted(self.targets.items())},
            "assignments": {
                key: value.as_dict() for key, value in sorted(self.assignments.items())
            },
            "sort_records": [record.as_dict() for record in self.sort_records],
            "rebook_orders": {
                key: value.as_dict() for key, value in sorted(self.rebook_orders.items())
            },
            "decisions": [decision.as_dict() for decision in self.decisions],
            "line_commands": {
                key: value.as_dict() for key, value in sorted(self.line_commands.items())
            },
            "recovery_tasks": {
                key: value.as_dict() for key, value in sorted(self.recovery_tasks.items())
            },
            "divert_points": dict(sorted(self.divert_points.items())),
            "transfer_commands": [command.as_dict() for command in self.transfer_commands],
            "load_records": [record.as_dict() for record in self.load_records],
            "audit": [entry.as_dict() for entry in self.audit],
            "quota_limits": dict(sorted(self.quota_limits.items())),
            "counters": dict(sorted(self.counters.items())),
            "sequence": self.sequence.as_dict(),
            "recheck_queue": list(self.recheck_queue),
            "belt": {
                "carousel": self.belt_carousel,
                "batch": self.belt_batch,
                "on_carousel": list(self.belt_on_carousel),
            },
        }

def apply_entry(state: State, kind: str, payload: dict[str, Any]) -> None:
    """Apply one journal command to ``state`` in place."""

    if kind == "flight.put":
        flight = Flight.from_dict(payload["flight"])
        state.flights[flight.id] = flight
    elif kind == "flight.remove":
        state.flights.pop(str(payload["flight_id"]), None)
        retired = str(payload["flight_id"])
        state.mappings = {
            bag_id: flight_id
            for bag_id, flight_id in state.mappings.items()
            if flight_id != retired
        }
        state.targets = {
            bag_id: target.evolved(active=False) if target.flight_id == retired else target
            for bag_id, target in state.targets.items()
        }
    elif kind == "chute.assign":
        assignment = ChuteAssignment.from_dict(payload)
        state.assignments[assignment.flight_id] = assignment
    elif kind == "bag.put":
        bag = Bag.from_dict(payload["bag"])
        state.bags[bag.id] = bag
    elif kind == "bag.remove":
        state.bags.pop(str(payload["bag_id"]), None)
        state.mappings.pop(str(payload["bag_id"]), None)
        state.targets.pop(str(payload["bag_id"]), None)
    elif kind == "mapping.put":
        state.mappings[str(payload["bag_id"])] = str(payload["flight_id"])
    elif kind == "target.put":
        target = FlightTarget.from_dict(payload["target"])
        if target.active:
            for key, previous in list(state.targets.items()):
                if previous.bag_id == target.bag_id and previous.active:
                    retired = previous.evolved(active=False)
                    state.targets[f"{target.bag_id}:v{retired.version}"] = retired
                    if key != target.bag_id:
                        state.targets.pop(key, None)
            state.targets[target.bag_id] = target
        else:
            state.targets[f"{target.bag_id}:v{target.version}"] = target
    elif kind == "sequence.put":
        state.sequence = Sequence.from_dict(payload)
    elif kind == "sort.record":
        state.sort_records.append(SortRecord.from_dict(payload["record"]))
    elif kind == "sort.plan":
        state.sort_records.append(SortRecord.from_dict(payload["record"]))
    elif kind == "sort.land":
        bag_id = str(payload["bag_id"])
        cycle = int(payload["cycle"])
        state.sort_records = [
            record.evolved(sort_state=SortState.LANDED)
            if record.bag_id == bag_id and record.cycle == cycle
            and record.sort_state is SortState.PLANNED
            else record
            for record in state.sort_records
        ]
    elif kind == "sort.reverse":
        bag_id = str(payload["bag_id"])
        at = str(payload.get("at", ""))
        reason = str(payload.get("reason", ""))
        chute_id = str(payload.get("chute_id", ""))

        def _reverse_match(record):
            if record.bag_id != bag_id or not record.sort_state.effective:
                return False
            if record.cycle != int(payload.get("cycle", record.cycle)):
                return False
            # When a chute is named the reverse targets that allocation only,
            # so a compensating rollback cannot touch an unrelated record.
            return not chute_id or record.chute_id == chute_id

        state.sort_records = [
            record.evolved(sort_state=SortState.REVERSED, reversed_at=at, reversed_reason=reason)
            if _reverse_match(record)
            else record
            for record in state.sort_records
        ]
    elif kind == "sort.restore":
        bag_id = str(payload["bag_id"])
        reason = str(payload.get("reason", ""))
        state.sort_records = [
            record.evolved(sort_state=SortState.PLANNED, reversed_at="", reversed_reason="")
            if record.bag_id == bag_id
            and record.sort_state is SortState.REVERSED
            and record.reversed_reason == reason
            and record.cycle == int(payload.get("cycle", record.cycle))
            else record
            for record in state.sort_records
        ]
    elif kind == "line.divert_point":
        state.divert_points[str(payload["chute_id"])] = int(payload["mile"])
    elif kind == "rebook.put":
        order = RebookOrder.from_dict(payload["order"])
        state.rebook_orders[order.id] = order
    elif kind == "rebook.decide":
        order_id = str(payload["rebook_id"])
        order = state.rebook_orders.get(order_id)
        if order is not None:
            state.rebook_orders[order_id] = order.evolved(decided=True)
    elif kind == "decision.append":
        state.decisions.append(BagDecision.from_dict(payload["decision"]))
    elif kind == "command.put":
        command = LineCommand.from_dict(payload["command"])
        state.line_commands[command.key] = command
    elif kind == "recovery.put":
        task = RecoveryTask.from_dict(payload["task"])
        state.recovery_tasks[task.bag_id] = task
    elif kind == "recovery.recover":
        bag_id = str(payload["bag_id"])
        task = state.recovery_tasks.get(bag_id)
        if task is not None:
            state.recovery_tasks[bag_id] = task.evolved(recovered=True)
    elif kind == "transfer.append":
        state.transfer_commands.append(TransferCommand.from_dict(payload["command"]))
    elif kind == "transfer.clear_before":
        floor = int(payload["batch"])
        state.transfer_commands = [
            command for command in state.transfer_commands if command.batch >= floor
        ]
    elif kind == "load.append":
        state.load_records.append(LoadRecord.from_dict(payload["record"]))
    elif kind == "audit.append":
        state.audit.append(AuditEntry.from_dict(payload["entry"]))
    elif kind == "quota.set":
        state.quota_limits[str(payload["flight_id"])] = int(payload["limit"])
    elif kind == "quota.adjust":
        flight_id = str(payload["flight_id"])
        base = state.quota_limits.get(flight_id, 0)
        state.quota_limits[flight_id] = base + int(payload["delta"])
    elif kind == "counter.add":
        name = str(payload["name"])
        state.counters[name] = state.counters.get(name, 0) + int(payload["delta"])
    elif kind == "recheck.enqueue":
        bag_id = str(payload["bag_id"])
        if bag_id not in state.recheck_queue:
            state.recheck_queue.append(bag_id)
    elif kind == "recheck.dequeue":
        bag_id = str(payload["bag_id"])
        state.recheck_queue = [item for item in state.recheck_queue if item != bag_id]
    elif kind == "recheck.clear":
        state.recheck_queue = []
    elif kind == "belt.set":
        state.belt_carousel = str(payload.get("carousel", ""))
        state.belt_batch = int(payload.get("batch", 0))
    elif kind == "belt.place":
        bag_id = str(payload["bag_id"])
        if bag_id not in state.belt_on_carousel:
            state.belt_on_carousel.append(bag_id)
    elif kind == "belt.clear":
        state.belt_on_carousel = []
        state.belt_carousel = str(payload.get("carousel", state.belt_carousel))
        state.belt_batch = int(payload.get("batch", state.belt_batch))
    else:  # pragma: no cover - guards against a typo in a new command kind
        raise ValidationError(f"unknown journal command {kind!r}")


def fold(entries: list[tuple[str, dict[str, Any]]]) -> State:
    """Rebuild a :class:`State` from a sequence of ``(kind, payload)`` pairs."""

    state = State()
    for kind, payload in entries:
        apply_entry(state, kind, payload)
    return state
