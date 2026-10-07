"""Last minute rebooking: one decision per bag, committed atomically.

The service stitches the three pieces the line must not let drift apart:

* **in-transit adjudication** (:mod:`bagsort.line`) says where the bag is and
  whether the new flight's divert point is still reachable;
* **route recompute** (:mod:`bagsort.sorter`) resolves the new flight to its
  current chute and writes a fresh, cycle-scoped allocation;
* **sort records** (:mod:`bagsort.sorter.records`) are reversed in the *same*
  transaction the new target goes live in.

Policy choice (recorded here on purpose, exposed as :data:`POLICY`): **option B
— let already-sorted bags finish, adjust everything still adjustable.** Bags at
induction are retargeted in place; bags on the line are rerouted only when the
divert point is still reachable inside the safety margin; bags past the point
keep flying to their original chute and open a manual recovery task naming that
chute. The opposite option — blanket recall of every in-transit bag before
re-sorting — trades a simpler "everything moves" story for a heavy time penalty
and guaranteed double handling of bags already seconds from their chute. Option
B minimises repeat sortation and protects the new flight's cutoff; its cost is
unreachable bags needing recovery, covered by the recovery task and the
pre-flight passenger-bag reconciliation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bagsort.clock import Clock
from bagsort.errors import ConflictError, OrderingError, ValidationError
from bagsort.ids import IdFactory
from bagsort.line.tracker import LineTracker
from bagsort.model.entities import (
    Bag,
    BagDecision,
    FlightTarget,
    LineCommand,
    RebookOrder,
    RecoveryTask,
)
from bagsort.model.enums import (
    BagLocation,
    BagState,
    CommandState,
    RebookAction,
    SortState,
)
from bagsort.model.errors import unknown_bag
from bagsort.model.validate import is_open_state
from bagsort.flight.mapping import BagMapping
from bagsort.sorter.route import RouteDetail
from bagsort.sorter.sorter import Sorter
from bagsort.store.store import Store

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bagsort.model.state import State

# Policy identifier stamped into every decision so audit reports the chosen
# orientation even after a future change.
POLICY = "adjust-pending-let-sorted-finish"

# The two orientations the operational brief asked to choose between. The
# rejected option is retained verbatim so an auditor can see both the decision
# and every trade it was made against, not just the winner.
POLICY_OPTIONS = {
    "chosen": POLICY,
    "options": [
        {
            "id": POLICY,
            "name": "adjust only what is still adjustable; let sorted bags finish",
            "chosen": True,
            "timeliness": "best: no whole-line stop, new-flight cutoff protected",
            "repeat_sortation": "minimum: only bags that physically land and are "
            "later recovered get a second sort cycle",
            "double_handling": "limited to unreachable bags, tracked as recovery tasks",
            "cutoff_risk": "a rerouted bag still re-clears the new flight's divert "
            "point inside the safety margin, else it is not promised",
            "cost": "unreachable bags fly to the old chute and must be pulled on "
            "arrival (passenger-bag reconciliation gate)",
        },
        {
            "id": "recall-all-in-transit-before-resort",
            "name": "hold and recall every in-transit bag, then re-sort all",
            "chosen": False,
            "timeliness": "worst: the line holds while every bag is pulled back",
            "repeat_sortation": "maximum: bags seconds from a chute are still "
            "handled twice",
            "double_handling": "every in-transit bag, including interceptable ones",
            "cutoff_risk": "high: the recall wave can miss the new flight cutoff",
            "cost": "simpler uniform story, but paid in delay and repeat handling",
        },
    ],
    "invariants": [
        "at most one active bag-to-flight target at any commit point",
        "target swap, allocation rollback and re-plan commit in one transaction",
        "an outcome report never executes a bag twice (idempotent on rebook id)",
        "a reroute the line rejects is compensated back to the old flight atomically",
        "a bag past the divert point keeps flying and is recorded to its landing chute",
    ],
}


class RebookService:
    """Accepts re-schedules and turns them into committed per-bag decisions."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        line: LineTracker,
        sorter: Sorter,
        mapping: BagMapping,
        ids: IdFactory | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._line = line
        self._sorter = sorter
        self._mapping = mapping
        self._ids = ids or IdFactory("rbk")

    # -- order intake ------------------------------------------------------

    def submit(self, bag_id: str, new_flight_id: str) -> RebookOrder:
        """Land a re-schedule and adjudicate it immediately, in one commit."""

        state = self._store.state()
        bag = state.bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        if new_flight_id not in state.flights:
            raise ValidationError(f"target flight {new_flight_id!r} is not registered")
        if not is_open_state(state.flights[new_flight_id].state):
            raise OrderingError(f"flight {new_flight_id!r} is not accepting baggage")
        old_flight_id = state.mappings.get(bag_id, "")
        if not old_flight_id:
            raise OrderingError(f"bag {bag_id!r} has no current flight to rebook")
        if old_flight_id == new_flight_id:
            raise OrderingError(f"bag {bag_id!r} already belongs to {new_flight_id!r}")
        if bag.state is BagState.LOADED:
            raise OrderingError(f"bag {bag_id!r} is already loaded and cannot be rebooked")

        order = RebookOrder(
            id=self._ids.next(),
            bag_id=bag_id,
            old_flight_id=old_flight_id,
            new_flight_id=new_flight_id,
            at=self._clock.now_iso(),
        )
        with self._store.session() as session:
            session.state.rebook_orders[order.id] = order
            session.record("rebook.put", {"order": order.as_dict()}, order.at)
        self._adjudicate(order)
        return self._store.state().rebook_orders[order.id]

    def get_order(self, rebook_id: str) -> RebookOrder:
        order = self._store.state().rebook_orders.get(rebook_id)
        if order is None:
            raise ValidationError(f"rebook order {rebook_id!r} is not known")
        return order

    def orders(self) -> list[RebookOrder]:
        return [self._store.state().rebook_orders[key] for key in sorted(self._store.state().rebook_orders)]

    def policy_record(self) -> dict[str, object]:
        """The chosen orientation and both options' trade-offs, on record."""

        return dict(POLICY_OPTIONS)

    # -- adjudication ------------------------------------------------------

    def _adjudicate(self, order: RebookOrder) -> BagDecision:
        if order.decided:
            raise ConflictError(f"rebook {order.id!r} has already been adjudicated")
        state = self._store.state()
        bag = state.bags[order.bag_id]
        detail = self._sorter.route_for(order.bag_id, order.new_flight_id)
        at = self._clock.now_iso()

        if bag.location is BagLocation.INDUCTION:
            decision = self._decide(
                order, bag, detail, RebookAction.RETARGET, True,
                "still at induction; target swapped before lane entry", at,
            )
            with self._store.session() as session:
                self._retarget(session, order, bag, decision, at)
            return decision

        if bag.location is BagLocation.LINE:
            feasible, why, divert_point = self._line.can_intercept(bag, detail.chute_id)
            if feasible:
                decision = self._decide(
                    order, bag, detail, RebookAction.REROUTE, True, why, at, divert_point,
                )
                with self._store.session() as session:
                    self._reroute(session, order, bag, detail, decision, at)
                return decision
            decision = self._decide(
                order, bag, detail, RebookAction.CONTINUE, False, why, at, divert_point,
            )
            with self._store.session() as session:
                self._continue(session, order, bag, decision, at)
            return decision

        decision = self._decide(
            order, bag, detail, RebookAction.CONTINUE, False,
            f"bag already landed in {bag.chute_id!r}; control cannot recall it", at,
        )
        with self._store.session() as session:
            self._continue(session, order, bag, decision, at)
        return decision

    def _decide(
        self,
        order: RebookOrder,
        bag: Bag,
        detail: RouteDetail,
        action: RebookAction,
        feasible: bool,
        reason: str,
        at: str,
        divert_point: int = 0,
    ) -> BagDecision:
        reached = action in (RebookAction.RETARGET, RebookAction.REROUTE)
        return BagDecision(
            rebook_id=order.id,
            bag_id=bag.id,
            old_flight_id=order.old_flight_id,
            new_flight_id=order.new_flight_id,
            action=action,
            location=bag.location,
            mile=bag.mile,
            divert_point=divert_point or self._safe_divert_point(detail.chute_id),
            target_chute=detail.chute_id if reached else "",
            feasible=feasible,
            reason=reason,
            at=at,
        )

    # -- the three mutually exclusive outcomes -----------------------------

    def _retarget(self, session, order: RebookOrder, bag: Bag, decision: BagDecision, at: str) -> None:
        """Induction bag: swap the target only; no allocation exists yet."""

        self._apply_target(session, bag, order.new_flight_id, at)
        updated = session.state.bags[bag.id].evolved(flight_id=order.new_flight_id)
        session.state.bags[bag.id] = updated
        session.record("bag.put", {"bag": updated.as_dict()}, at)
        self._close(session, order, decision, at)

    def _reroute(
        self,
        session,
        order: RebookOrder,
        bag: Bag,
        detail: RouteDetail,
        decision: BagDecision,
        at: str,
    ) -> None:
        """On the line, still interceptable: swap the target and re-plan.

        If an allocation is already live it is reversed first; a bag that
        entered the line before any allocation was issued simply gets the new
        target and plan, with nothing to roll back.
        """

        self._sorter.reverse_in(session, bag, f"reroute:{order.id}", at)
        self._apply_target(session, bag, order.new_flight_id, at)
        updated = session.state.bags[bag.id].evolved(flight_id=order.new_flight_id)
        session.state.bags[bag.id] = updated
        session.record("bag.put", {"bag": updated.as_dict()}, at)
        self._sorter.plan_in(session, updated, detail, at)
        command = LineCommand(
            key=f"reroute:{order.id}",
            bag_id=bag.id,
            kind="reroute",
            rebook_id=order.id,
            chute_id=detail.chute_id,
            state=CommandState.PENDING,
            at=at,
        )
        session.state.line_commands[command.key] = command
        session.record("command.put", {"command": command.as_dict()}, at)
        self._close(session, order, decision, at)

    def _continue(
        self, session, order: RebookOrder, bag: Bag, decision: BagDecision, at: str
    ) -> None:
        """Past the point: keep the old target valid and open a recovery task."""

        # The mapping deliberately stays on the old flight: the bag is still
        # physically committed to it. Recovery is what later flips the bag, so
        # mapping and physical custody never disagree.
        chute = bag.chute_id or self._last_live_chute(session, bag.id)
        task = RecoveryTask(
            bag_id=bag.id,
            flight_id=order.old_flight_id,
            new_flight_id=order.new_flight_id,
            chute_id=chute,
            rebook_id=order.id,
            at=at,
        )
        session.state.recovery_tasks[bag.id] = task
        session.record("recovery.put", {"task": task.as_dict()}, at)
        self._close(session, order, decision, at)

    # -- physical command outcome and recovery -----------------------------

    def command_outcome(
        self, rebook_id: str, accepted: bool, detail: str = ""
    ) -> LineCommand:
        """Report what the physical line did with a reroute command.

        Idempotent on the rebook id: retried outcome reports never execute a bag
        twice. A rejected reroute cannot silently claim success, so it opens the
        same recovery task a too-late adjudication takes. The original decision
        stays immutable for audit; the command record carries the truth.
        """

        key = f"reroute:{rebook_id}"
        state = self._store.state()
        command = state.line_commands.get(key)
        if command is None:
            raise ValidationError(f"no line command for rebook {rebook_id!r}")
        if command.state is not CommandState.PENDING:
            return command
        at = self._clock.now_iso()
        with self._store.session() as session:
            updated = command.evolved(
                state=CommandState.ACCEPTED if accepted else CommandState.REJECTED,
                attempts=command.attempts + 1,
                detail=detail,
                at=at,
            )
            session.state.line_commands[key] = updated
            session.record("command.put", {"command": updated.as_dict()}, at)
            if not accepted:
                order = session.state.rebook_orders[rebook_id]
                bag = session.state.bags[command.bag_id]
                self._undo_reroute(
                    session, order, bag, at=at, chute_id=command.chute_id
                )
                task = RecoveryTask(
                    bag_id=bag.id,
                    flight_id=order.old_flight_id,
                    new_flight_id=order.new_flight_id,
                    chute_id=command.chute_id or bag.chute_id,
                    rebook_id=rebook_id,
                    at=at,
                )
                session.state.recovery_tasks[bag.id] = task
                session.record("recovery.put", {"task": task.as_dict()}, at)
                session.record(
                    "counter.add", {"name": "rebook.reroute_rejected", "delta": 1}, at
                )
            else:
                session.record(
                    "counter.add", {"name": "rebook.reroute_accepted", "delta": 1}, at
                )
        return self._store.state().line_commands[key]

    def _undo_reroute(
        self, session, order: RebookOrder, bag: Bag, at: str, chute_id: str
    ) -> None:
        """Compensate a reroute the physical line would not execute.

        Expressed only as journal commands so the rollback survives replay:

        * reverse the new-flight plan;
        * restore as planned the allocation that was reversed at adjudication;
        * flip the single active target back to the old flight.

        It commits as one transaction, so the bag can never be left on the new
        flight in the data while physically still bound for the old one.
        """

        prior = f"reroute:{order.id}"
        has_prior = any(
            record.bag_id == bag.id
            and record.cycle == bag.cycle
            and record.sort_state is SortState.REVERSED
            and record.reversed_reason == prior
            for record in session.state.sort_records
        )
        if not has_prior:
            raise ConflictError(
                f"reroute rollback for bag {bag.id!r} could not find the prior allocation"
            )
        # Order matters: bring the old allocation back first (it is currently
        # reversed), then reverse the new-flight plan. Reversing first would
        # flip both effective records.
        session.record(
            "sort.restore",
            {"bag_id": bag.id, "cycle": bag.cycle, "reason": prior},
            at,
        )
        session.record(
            "sort.reverse",
            {
                "bag_id": bag.id,
                "cycle": bag.cycle,
                "reason": "reroute_rejected",
                "chute_id": chute_id,
                "at": at,
            },
            at,
        )
        current = self._latest_target(session, bag.id)
        restored_target = FlightTarget(
            bag_id=bag.id,
            flight_id=order.old_flight_id,
            version=(current.version + 1) if current else 1,
            active=True,
            at=at,
        )
        session.record("target.put", {"target": restored_target.as_dict()}, at)
        session.state.mappings[bag.id] = order.old_flight_id
        session.record(
            "mapping.put", {"bag_id": bag.id, "flight_id": order.old_flight_id}, at
        )
        old_bag = session.state.bags[bag.id].evolved(flight_id=order.old_flight_id)
        session.state.bags[bag.id] = old_bag
        session.record("bag.put", {"bag": old_bag.as_dict()}, at)

    def recover(self, bag_id: str) -> Bag:
        """Pull a too-late bag from its original chute and re-induct it.

        This is the one and only place the bag flips from the old flight to the
        new one after a CONTINUE decision, so mapping and physical custody move
        together and the bag cannot show two valid flights at once. The new
        sortation cycle starts here; dispatch will not accept the bag twice in
        the same cycle.
        """

        state = self._store.state()
        task = state.recovery_tasks.get(bag_id)
        if task is None:
            raise ConflictError(f"bag {bag_id!r} has no open recovery task")
        if task.recovered:
            raise ConflictError(f"bag {bag_id!r} has already been recovered")
        order = state.rebook_orders[task.rebook_id]
        bag = state.bags[bag_id]
        if bag.location is not BagLocation.LANDED:
            raise ConflictError(f"bag {bag_id!r} has not landed and cannot be recovered yet")
        at = self._clock.now_iso()
        with self._store.session() as session:
            recovered = session.state.bags[bag_id].evolved(
                location=BagLocation.RECOVERY,
                state=BagState.RECHECKED,
            )
            session.state.bags[bag_id] = recovered
            session.record("bag.put", {"bag": recovered.as_dict()}, at)
            session.state.recovery_tasks[bag_id] = task.evolved(recovered=True)
            session.record(
                "recovery.recover",
                {"bag_id": bag_id, "rebook_id": order.id},
                at,
            )
            self._apply_target(session, recovered, order.new_flight_id, at)
            reinducted = session.state.bags[bag_id].evolved(
                location=BagLocation.INDUCTION,
                mile=0,
                chute_id="",
                flight_id=order.new_flight_id,
                state=BagState.INJECTED,
                cycle=recovered.cycle + 1,
            )
            session.state.bags[bag_id] = reinducted
            session.record("bag.put", {"bag": reinducted.as_dict()}, at)
            if bag_id not in session.state.recheck_queue:
                session.state.recheck_queue.append(bag_id)
                session.record("recheck.enqueue", {"bag_id": bag_id}, at)
            session.record(
                "counter.add", {"name": "rebook.recovered", "delta": 1}, at
            )
        return self._store.state().bags[bag_id]

    # -- read side ---------------------------------------------------------

    def decision_for(self, rebook_id: str) -> BagDecision:
        return self._decision_for(self._store.state(), rebook_id)

    def decisions_for_bag(self, bag_id: str) -> list[BagDecision]:
        return [
            decision
            for decision in self._store.state().decisions
            if decision.bag_id == bag_id
        ]

    def decisions(self) -> list[BagDecision]:
        return list(self._store.state().decisions)

    def effective_action(self, rebook_id: str) -> RebookAction:
        """The conclusion after physical feedback, which the console reports."""

        decision = self.decision_for(rebook_id)
        command = self.command(rebook_id)
        if command is not None and command.state is CommandState.REJECTED:
            return RebookAction.CONTINUE
        return decision.action

    def pending_commands(self) -> list[LineCommand]:
        return [
            command
            for command in self._store.state().line_commands.values()
            if command.state is CommandState.PENDING
        ]

    def command(self, rebook_id: str) -> LineCommand | None:
        return self._store.state().line_commands.get(f"reroute:{rebook_id}")

    def recovery_tasks(self, include_done: bool = False) -> list[RecoveryTask]:
        tasks = list(self._store.state().recovery_tasks.values())
        if not include_done:
            tasks = [task for task in tasks if not task.recovered]
        return sorted(tasks, key=lambda task: (task.at, task.bag_id))

    # -- reconciliation: console conclusion must match audit evidence ------

    def reconciliation(self, audit_entries=None) -> dict[str, object]:
        """Cross-check decisions, commands, targets, recovery tasks and audit.

        When ``audit_entries`` are supplied (the hash-linked audit chain), the
        report also verifies that every decision the console would return has a
        corresponding audit entry carrying the same rebook id and action, which
        is the "console conclusion matches the audit trail" guarantee.
        """

        state = self._store.state()
        issues: list[str] = []
        audit_by_rebook: dict[str, set[str]] = {}
        if audit_entries is not None:
            for entry in audit_entries:
                rebook_id = entry.detail.get("rebook_id")
                if rebook_id:
                    audit_by_rebook.setdefault(str(rebook_id), set()).add(
                        str(entry.detail.get("action", ""))
                    )
        for decision in state.decisions:
            bag_id = decision.bag_id
            target = self._mapping.active_target(bag_id)
            command = state.line_commands.get(f"reroute:{decision.rebook_id}")
            task = state.recovery_tasks.get(bag_id)
            effective = self.effective_action(decision.rebook_id)
            if audit_entries is not None:
                audited_actions = audit_by_rebook.get(decision.rebook_id, set())
                if effective.value not in audited_actions and decision.action.value not in audited_actions:
                    issues.append(
                        f"{bag_id}: decision {effective.value!r} has no matching audit entry"
                    )
            if decision.action is RebookAction.RETARGET:
                if target is None or target.flight_id != decision.new_flight_id:
                    issues.append(f"{bag_id}: retarget without a single active new target")
            if decision.action is RebookAction.REROUTE:
                if command is None:
                    issues.append(f"{bag_id}: reroute decision without a line command")
                elif command.state is CommandState.REJECTED and task is None:
                    issues.append(f"{bag_id}: rejected reroute without a recovery task")
                elif command.state is CommandState.ACCEPTED and (
                    target is None or target.flight_id != decision.new_flight_id
                ):
                    issues.append(f"{bag_id}: accepted reroute without an active new target")
            if decision.action is RebookAction.CONTINUE and task is None:
                issues.append(f"{bag_id}: continue decision without a recovery task")
        active_targets = [target for target in state.targets.values() if target.active]
        if len(active_targets) != len({target.bag_id for target in active_targets}):
            issues.append("a bag carries more than one active target")
        for bag_id, mapping_flight in state.mappings.items():
            target = state.targets.get(bag_id)
            if target is None or not target.active or target.flight_id != mapping_flight:
                issues.append(f"{bag_id}: mapping and active target disagree")
        return {
            "policy": POLICY,
            "decisions": len(state.decisions),
            "pending_commands": len(self.pending_commands()),
            "open_recovery": len(self.recovery_tasks()),
            "issues": issues,
            "ok": not issues,
        }

    # -- helpers -----------------------------------------------------------

    def _close(
        self, session, order: RebookOrder, decision: BagDecision, at: str
    ) -> None:
        session.state.rebook_orders[order.id] = order.evolved(decided=True)
        session.record("rebook.decide", {"rebook_id": order.id}, at)
        session.state.decisions.append(decision)
        session.record("decision.append", {"decision": decision.as_dict()}, at)

    def _apply_target(self, session, bag: Bag, new_flight_id: str, at: str) -> None:
        previous = self._latest_target(session, bag.id)
        target = FlightTarget(
            bag_id=bag.id,
            flight_id=new_flight_id,
            version=(previous.version + 1) if previous else 1,
            active=True,
            at=at,
        )
        # The reducer retires the previous active row to a version-scoped key.
        session.record("target.put", {"target": target.as_dict()}, at)
        session.state.mappings[bag.id] = new_flight_id
        session.record("mapping.put", {"bag_id": bag.id, "flight_id": new_flight_id}, at)

    def _latest_target(self, session, bag_id: str):
        versions = [
            target
            for target in session.state.targets.values()
            if target.bag_id == bag_id
        ]
        return max(versions, key=lambda target: target.version, default=None)

    def _last_live_chute(self, session, bag_id: str) -> str:
        chute = ""
        for record in session.state.sort_records:
            if record.bag_id == bag_id and record.sort_state.effective:
                chute = record.chute_id
        return chute

    def _decision_for(self, state: "State", rebook_id: str) -> BagDecision:
        for decision in reversed(state.decisions):
            if decision.rebook_id == rebook_id:
                return decision
        raise ValidationError(f"no decision recorded for rebook {rebook_id!r}")

    def _safe_divert_point(self, chute_id: str) -> int:
        try:
            return self._line.divert_point(chute_id)
        except ConflictError:
            return 0
