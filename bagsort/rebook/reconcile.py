"""Reconciliation: the console verdict and the audit trail must agree.

Every rebook conclusion is persisted as an order and written into the hash
chained audit trail. This module re-derives the audit side from the entries
alone and compares it with the stored order, then runs the cross-subsystem
invariants (single active target, single physical sort, mapping/target
agreement). A verdict the audit cannot reproduce is a failure, not a display
quirk.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.line.enums import LinePhase, RebookVerdict, TargetStatus
from bagsort.sorter.records import SortLog
from bagsort.store.store import Store


@dataclass(frozen=True)
class OrderReconciliation:
    order_id: str
    matches: bool
    stored_verdict: str
    audit_verdict: str
    audit_entries: int
    problems: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "matches": self.matches,
            "stored_verdict": self.stored_verdict,
            "audit_verdict": self.audit_verdict,
            "audit_entries": self.audit_entries,
            "problems": list(self.problems),
        }


class Reconciler:
    """Cross-checks rebook orders against audit entries and invariants."""

    _AUDIT_KIND = {
        RebookVerdict.SWAPPED_AT_INDUCTION.value: "rebook_induction_swap",
        RebookVerdict.RECALLED.value: "rebook_recall",
        RebookVerdict.LET_RIDE.value: "rebook_let_ride",
    }

    def __init__(self, store: Store) -> None:
        self._store = store

    def reconcile_order(self, order_id: str) -> OrderReconciliation:
        state = self._store.state()
        order = state.rebook_orders[order_id]
        entries = [
            entry
            for entry in state.audit
            if entry.subject == order.bag_id
            and entry.detail.get("request_id") == order.request_id
        ]
        problems: list[str] = []
        audit_verdict = ""
        if not entries:
            problems.append("no audit entry carries this request id")
        else:
            kinds = {entry.kind for entry in entries}
            expected = self._AUDIT_KIND.get(order.verdict, "")
            matching = [entry for entry in entries if entry.kind == expected]
            if not matching:
                audit_verdict = sorted(kinds)[0]
                problems.append(
                    f"audit says {audit_verdict!r}, order says {order.verdict!r}"
                )
            else:
                audit_verdict = matching[-1].kind
            detail = entries[-1].detail
            if str(detail.get("new_flight_id", "")) != order.new_flight_id:
                problems.append("audit new_flight_id disagrees with the order")
            if str(detail.get("old_flight_id", "")) != order.old_flight_id:
                problems.append("audit old_flight_id disagrees with the order")
        matches = not problems
        return OrderReconciliation(
            order_id=order_id,
            matches=matches,
            stored_verdict=order.verdict,
            audit_verdict=audit_verdict,
            audit_entries=len(entries),
            problems=tuple(problems),
        )

    def reconcile_all(self) -> list[OrderReconciliation]:
        return [
            self.reconcile_order(order_id)
            for order_id in sorted(self._store.state().rebook_orders)
        ]

    def integrity_report(self) -> dict[str, object]:
        """The cross-subsystem invariants, evaluated independently of services."""

        state = self._store.state()
        violations: list[str] = []
        targets_by_bag: dict[str, list[str]] = {}
        for target in state.targets.values():
            targets_by_bag.setdefault(target.bag_id, []).append(target.status)
        for bag_id, statuses in targets_by_bag.items():
            active = [status for status in statuses if status == TargetStatus.ACTIVE.value]
            if len(active) > 1:
                violations.append(f"{bag_id}: {len(active)} active targets")
            mapping_flight = state.mappings.get(bag_id)
            active_targets = [
                target
                for target in state.targets.values()
                if target.bag_id == bag_id and target.status == TargetStatus.ACTIVE.value
            ]
            if active_targets and mapping_flight != active_targets[0].flight_id:
                violations.append(f"{bag_id}: mapping and active target disagree")
            if not active_targets and bag_id in state.mappings:
                violations.append(f"{bag_id}: mapping exists without an active target")
        state = self._store.state()
        sort_log = SortLog(self._store)
        landed: dict[str, int] = {}
        for record in sort_log.effective_records():
            landed[record.bag_id] = landed.get(record.bag_id, 0) + 1
        pending = sum(1 for bag_id in state.line if sort_log.pending_for(bag_id) is not None)
        for bag_id, count in landed.items():
            if count > 1:
                violations.append(f"{bag_id}: {count} effective sort records (double sort)")
        live_commands: dict[str, int] = {}
        for command in state.divert_commands.values():
            if command.state in ("armed", "cancel_pending"):
                live_commands[command.bag_id] = live_commands.get(command.bag_id, 0) + 1
        for bag_id, count in live_commands.items():
            if count > 1:
                violations.append(f"{bag_id}: {count} live divert commands")
        for tracking in state.line.values():
            if tracking.phase == LinePhase.ON_LINE.value and tracking.bag_id not in state.bags:
                violations.append(f"{tracking.bag_id}: on the line without a bag record")
        return {
            "violations": violations,
            "holds": not violations,
            "rebook_orders": len(state.rebook_orders),
            "targets": len(state.targets),
            "effective_sort_records": len(landed),
            "armed_allocations": pending,
            "live_divert_commands": sum(live_commands.values()),
            "audit_intact": self._audit_intact(),
        }

    def _audit_intact(self) -> bool:
        """Local chain verification so reconciliation does not depend on wiring."""

        from bagsort.audit.chain import GENESIS, digest_of

        previous = GENESIS
        for entry in self._store.state().audit:
            if entry.prev_digest != previous or digest_of(entry) != entry.digest:
                return False
            previous = entry.digest
        return True
