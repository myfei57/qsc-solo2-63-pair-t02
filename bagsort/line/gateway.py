"""Physical gateway: the boundary between the log and the diverter hardware.

Every arm or cancel the service wants is posted here. A command only becomes
``armed`` / ``cancelled`` in the journal after the (simulated) PLC accepts it,
which is the prepare/confirm ordering a real cancel needs. ``set_strict`` lets
a scenario force a NACK so the compensating branch is testable. Retries post
the same command id and get the existing verdict back, so a redriven request
never double-arms a diverter.
"""

from __future__ import annotations

ACK = "ack"
NACK = "nack"


class LineGateway:
    """Deterministic stand-in for the PLC arm/cancel interface."""

    def __init__(self) -> None:
        self._strict: dict[str, str] = {}
        self._verdicts: dict[str, str] = {}

    def reset(self) -> None:
        self._strict.clear()
        self._verdicts.clear()

    def set_strict(self, bag_id: str, reject: str) -> None:
        """Force ``reject`` (``arm`` or ``cancel``) to NACK for one bag."""

        self._strict[bag_id] = reject

    def post_arm(self, command_id: str, bag_id: str, divert_at: int) -> str:
        return self._verdict(command_id, bag_id, "arm")

    def post_cancel(self, command_id: str, bag_id: str) -> str:
        return self._verdict(command_id, bag_id, "cancel")

    def _verdict(self, command_id: str, bag_id: str, action: str) -> str:
        key = (command_id, action)
        if key in self._verdicts:
            return self._verdicts[key]
        verdict = NACK if self._strict.get(bag_id) == action else ACK
        self._verdicts[key] = verdict
        return verdict
