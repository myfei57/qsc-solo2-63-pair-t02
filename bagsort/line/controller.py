"""Divert command state machine and its idempotency guard.

One bag has at most one live (``armed`` / ``cancel_pending``) diverter command.
Issuing another while one is live is refused, which, together with the sort
log, is what keeps a retried request from sending one bag twice. Command ids
are deterministic from (bag, target number), so a crash-and-redrive finds the
same row instead of creating a new one.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.model.entities import Bag, DivertCommand, SortRecord
from bagsort.store.store import Store
from .enums import DivertState, SortStatus


def divert_id(bag_id: str, target_number: int) -> str:
    return f"div-{bag_id}-{target_number}"


class DivertController:
    """Issues and settles diverter commands inside write transactions."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- read side --------------------------------------------------------

    def get(self, command_id: str) -> DivertCommand | None:
        return self._store.state().divert_commands.get(command_id)

    def for_bag(self, bag_id: str) -> list[DivertCommand]:
        return [
            self._store.state().divert_commands[key]
            for key in sorted(self._store.state().divert_commands)
            if key.startswith(f"div-{bag_id}-")
        ]

    def live_for(self, bag_id: str) -> DivertCommand | None:
        for command in self.for_bag(bag_id):
            if command.state in (DivertState.ARMED.value, DivertState.CANCEL_PENDING.value):
                return command
        return None

    # -- write side (call inside a store session) -------------------------

    def issue(
        self,
        session,
        bag: Bag,
        target_id: str,
        target_number: int,
        flight_id: str,
        chute_id: str,
        divert_at: int,
    ) -> DivertCommand:
        """Arm a diverter and allocate the (recallable) sort record together."""

        existing = self.live_for(bag.id)
        if existing is not None:
            raise ConflictError(
                f"bag {bag.id!r} already has a live divert command {existing.id!r}"
            )
        command_id = divert_id(bag.id, target_number)
        at = self._clock.now_iso()
        command = DivertCommand(
            id=command_id,
            bag_id=bag.id,
            target_id=target_id,
            flight_id=flight_id,
            chute_id=chute_id,
            divert_at=divert_at,
            state=DivertState.ARMED.value,
        )
        record = SortRecord(
            bag_id=bag.id,
            flight_id=flight_id,
            chute_id=chute_id,
            sequence=bag.sequence,
            epoch=bag.epoch,
            at=at,
            status=SortStatus.ALLOCATED.value,
            note="divert_armed",
        )
        session.state.divert_commands[command_id] = command
        session.state.sort_records.append(record)
        session.record("divert.put", {"command": command.as_dict()}, at)
        session.record("sort.record", {"record": record.as_dict()}, at)
        return command

    def stage_cancel(self, session, command: DivertCommand) -> DivertCommand:
        updated = command.evolved(state=DivertState.CANCEL_PENDING.value)
        session.state.divert_commands[updated.id] = updated
        session.record("divert.put", {"command": updated.as_dict()}, self._clock.now_iso())
        return updated

    def settle_cancel(self, session, command: DivertCommand, accepted: bool) -> DivertCommand:
        """Apply the PLC verdict to a staged cancel.

        An accepted cancel cancels the command and rolls the still-armed sort
        record back in the same transaction. A rejected cancel returns the
        command to ``armed``; the bag rides on and lands normally.
        """

        if command.state != DivertState.CANCEL_PENDING.value:
            raise ConflictError(f"divert command {command.id!r} is not awaiting a cancel verdict")
        at = self._clock.now_iso()
        if accepted:
            updated = command.evolved(state=DivertState.CANCELLED.value, reason="recall")
            session.state.divert_commands[updated.id] = updated
            session.record("divert.put", {"command": updated.as_dict()}, at)
            self._rollback_allocation(session, updated.bag_id, at, note="recall_rollback")
            return updated
        updated = command.evolved(state=DivertState.ARMED.value, reason="cancel_rejected")
        session.state.divert_commands[updated.id] = updated
        session.record("divert.put", {"command": updated.as_dict()}, at)
        return updated

    def mark_fired(self, session, command: DivertCommand) -> DivertCommand:
        updated = command.evolved(state=DivertState.FIRED.value)
        session.state.divert_commands[updated.id] = updated
        session.record("divert.put", {"command": updated.as_dict()}, self._clock.now_iso())
        return updated

    def _rollback_allocation(self, session, bag_id: str, at: str, note: str) -> None:
        session.state.sort_records = [
            (
                record.evolved(status=SortStatus.ROLLED_BACK.value, note=note)
                if record.bag_id == bag_id and record.status != SortStatus.LANDED.value
                else record
            )
            for record in session.state.sort_records
        ]
        session.record(
            "sort.rollback", {"bag_id": bag_id, "note": note, "at": at}, at
        )
