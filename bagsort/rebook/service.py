"""Rebook orchestration: the transaction boundary around a passenger change.

The flow for one bag is::

    evaluate (in-transit + geometry) -> prepare one journaled order
    -> physical gateway verdict -> settle, mapping + sort records in one tx

``PREPARED`` orders are crash recovery points: the cancel was staged to the
PLC but its verdict was not committed. Re-posting the same request id settles
the existing order instead of creating a second one, so neither a crash nor a
retried request can make one bag walk two flights or get sorted twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, OrderingError
from bagsort.line.enums import (
    LinePhase,
    RebookStatus,
    RebookVerdict,
    TargetStatus,
)
from bagsort.line.controller import DivertController
from bagsort.line.gateway import ACK
from bagsort.line.targets import TargetBoard
from bagsort.line.tracker import LineTracker
from bagsort.model.entities import Bag, RebookOrder
from bagsort.model.enums import BagState
from bagsort.recheck.queue import RecheckQueue
from bagsort.store.store import Store
from .planner import RerouteDecision, ReroutePlanner
from .policy import PolicyBook


@dataclass(frozen=True)
class RebookResult:
    order: RebookOrder
    policy_sealed: bool
    staged: bool = False

    @property
    def final(self) -> bool:
        return self.order.status == RebookStatus.SETTLED.value

    def as_dict(self) -> dict[str, object]:
        return {
            "order": self.order.as_dict(),
            "policy_sealed": self.policy_sealed,
            "staged": self.staged,
            "final": self.final,
        }


class RebookService:
    """Applies one rebook request end to end."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        book: object,
        planner: ReroutePlanner,
        targets: TargetBoard,
        controller: DivertController,
        tracker: LineTracker,
        gateway: object,
        policy: PolicyBook,
        recheck: RecheckQueue,
    ) -> None:
        self._store = store
        self._clock = clock
        self._book = book
        self._planner = planner
        self._targets = targets
        self._controller = controller
        self._tracker = tracker
        self._gateway = gateway
        self._policy = policy
        self._recheck = recheck

    # -- read side --------------------------------------------------------

    def order(self, order_id: str) -> RebookOrder:
        order = self._store.state().rebook_orders.get(order_id)
        if order is None:
            raise NotFoundError(f"rebook order {order_id!r} does not exist")
        return order

    def by_request(self, request_id: str) -> RebookOrder | None:
        order_id = self._store.state().rebook_requests.get(request_id)
        return self._store.state().rebook_orders.get(order_id) if order_id else None

    def orders_for(self, bag_id: str) -> list[RebookOrder]:
        return [
            self._store.state().rebook_orders[key]
            for key in sorted(self._store.state().rebook_orders)
            if self._store.state().rebook_orders[key].bag_id == bag_id
        ]

    def pending_orders(self) -> list[RebookOrder]:
        return [
            self._store.state().rebook_orders[key]
            for key in sorted(self._store.state().rebook_orders)
            if self._store.state().rebook_orders[key].status == RebookStatus.PREPARED.value
        ]

    def policy(self):
        return self._policy.current()

    # -- main entry -------------------------------------------------------

    def rebook(self, bag_id: str, new_flight_id: str, request_id: str) -> RebookResult:
        """Apply the rebook; idempotent on ``request_id``."""

        if not request_id or not request_id.strip():
            raise OrderingError("a rebook request needs a client request id")
        existing = self.by_request(request_id.strip())
        if existing is not None:
            return RebookResult(order=existing, policy_sealed=False)
        self._assert_open_rebook(bag_id, new_flight_id)
        decision = self._planner.evaluate(bag_id, new_flight_id)
        if decision.old_flight_id == new_flight_id:
            raise ConflictError(f"bag {bag_id!r} already targets {new_flight_id!r}")
        sealed = self._policy.seal()
        if decision.verdict is RebookVerdict.RECALLED:
            order = self._prepare_recall(decision, request_id.strip())
            return self._settle_recall(order, policy_sealed=not sealed.already_sealed)
        if decision.verdict is RebookVerdict.LET_RIDE and decision.phase is LinePhase.ON_LINE:
            order = self._prepare_let_ride(decision, request_id.strip())
            return RebookResult(
                order=order, policy_sealed=not sealed.already_sealed, staged=True
            )
        if decision.verdict is RebookVerdict.LET_RIDE:
            order = self._settle_let_ride_now(decision, request_id.strip())
            return RebookResult(order=order, policy_sealed=not sealed.already_sealed)
        order = self._settle_direct(decision, request_id.strip())
        return RebookResult(order=order, policy_sealed=not sealed.already_sealed)

    def settle_prepared(self, order_id: str) -> RebookResult:
        """Re-post the cancel verdict after a crash/retry and finish the order."""

        order = self.order(order_id)
        if order.status == RebookStatus.SETTLED.value:
            return RebookResult(order=order, policy_sealed=False)
        return self._settle_recall(order, policy_sealed=False)

    def settle_landing(self, bag_id: str) -> RebookOrder | None:
        """Finish a ``LET_RIDE`` order whose bag has now physically landed."""

        pending = [
            order
            for order in self.orders_for(bag_id)
            if order.status == RebookStatus.PREPARED.value
            and order.verdict == RebookVerdict.LET_RIDE.value
        ]
        if not pending:
            return None
        order = pending[-1]
        landed_chute = self._store.state().bags[bag_id].chute_id or order.landed_chute_id
        return self._finish_let_ride_entry(order, landed_chute, ride_completed=True)

    # -- branch: direct swap at the induction desk ------------------------

    def _settle_direct(self, decision: RerouteDecision, request_id: str) -> RebookOrder:
        at = self._clock.now_iso()
        with self._store.session() as session:
            target = self._targets.create(
                session,
                decision.bag_id,
                decision.new_flight_id,
                decision.new_chute_id,
                TargetStatus.ACTIVE,
                request_id,
                at,
            )
            self._targets.activate(session, decision.bag_id, target)
            bag = session.state.bags[decision.bag_id]
            updated = bag.evolved(flight_id=decision.new_flight_id)
            session.state.bags[decision.bag_id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, at)
            tracking = session.state.line.get(decision.bag_id)
            if tracking is not None and tracking.phase == LinePhase.RECALLED.value:
                recalled = tracking.evolved(phase=LinePhase.NOT_ENTERED.value, position=0)
                session.state.line[decision.bag_id] = recalled
                session.record("line.put", {"tracking": recalled.as_dict()}, at)
            order = self._record_order(
                session,
                decision,
                request_id,
                RebookStatus.SETTLED,
                decision.verdict,
                target_id=target.id,
                at=at,
            )
            session.record(
                "counter.add", {"name": "rebook.swapped_at_induction", "delta": 1}, at
            )
        return order

    # -- branch: let ride for a bag that has already landed ----------------

    def _settle_let_ride_now(self, decision: RerouteDecision, request_id: str) -> RebookOrder:
        at = self._clock.now_iso()
        with self._store.session() as session:
            target = self._targets.create(
                session,
                decision.bag_id,
                decision.new_flight_id,
                decision.new_chute_id,
                TargetStatus.PENDING,
                request_id,
                at,
            )
            bag = session.state.bags[decision.bag_id]
            landed_chute = bag.chute_id
            prepared = self._record_order(
                session,
                decision,
                request_id,
                RebookStatus.PREPARED,
                RebookVerdict.LET_RIDE,
                target_id=target.id,
                at=at,
            )
            order = self._finish_let_ride(
                session, prepared, landed_chute, ride_completed=True, at=at
            )
        return order

    # -- branch: two phase recall on the loop -----------------------------

    def _prepare_recall(self, decision: RerouteDecision, request_id: str) -> RebookOrder:
        assert decision.feasibility is not None
        at = self._clock.now_iso()
        with self._store.session() as session:
            target = self._targets.create(
                session,
                decision.bag_id,
                decision.new_flight_id,
                decision.new_chute_id,
                TargetStatus.PENDING,
                request_id,
                at,
            )
            live = self._controller.live_for(decision.bag_id)
            if live is None:
                raise ConflictError(f"bag {decision.bag_id!r} has no live divert to cancel")
            staged = self._controller.stage_cancel(session, live)
            order = self._record_order(
                session,
                decision,
                request_id,
                RebookStatus.PREPARED,
                RebookVerdict.PENDING,
                target_id=target.id,
                command_id=staged.id,
                at=at,
            )
        return order

    def _settle_recall(self, prepared: RebookOrder, policy_sealed: bool) -> RebookResult:
        command = self._controller.get(prepared.command_id)
        if command is None:
            raise ConflictError(f"divert command {prepared.command_id!r} vanished before settle")
        verdict = self._gateway.post_cancel(command.id, prepared.bag_id)
        accepted = verdict == ACK
        at = self._clock.now_iso()
        if accepted:
            with self._store.session() as session:
                self._controller.settle_cancel(session, command, True)
                bag = session.state.bags[prepared.bag_id]
                order = self._finish_recalled(session, prepared, bag, at)
            return RebookResult(order=order, policy_sealed=policy_sealed, staged=False)
        # Cancel rejected: the diverter is re-armed; the ride finishes at the
        # old chute and the order settles there. Stay PREPARED until landing.
        with self._store.session() as session:
            self._controller.settle_cancel(session, command, False)
            order = prepared.evolved(
                verdict=RebookVerdict.LET_RIDE.value,
                reason="cancel_rejected",
                at=at,
            )
            session.state.rebook_orders[order.id] = order
            session.record("rebook.put", {"order": order.as_dict()}, at)
        return RebookResult(order=order, policy_sealed=policy_sealed, staged=True)

    def _finish_recalled(
        self, session, prepared: RebookOrder, bag: Bag, at: str
    ) -> RebookOrder:
        """Cancel accepted: roll back is done; move the target and the mapping."""

        target = session.state.targets[prepared.target_id]
        self._targets.activate(session, prepared.bag_id, target)
        updated = bag.evolved(flight_id=prepared.new_flight_id, state=BagState.INJECTED)
        session.state.bags[prepared.bag_id] = updated
        session.record("bag.put", {"bag": updated.as_dict()}, at)
        tracking = session.state.line.get(prepared.bag_id)
        if tracking is not None:
            recalled = tracking.evolved(phase=LinePhase.RECALLED.value)
            session.state.line[prepared.bag_id] = recalled
            session.record("line.put", {"tracking": recalled.as_dict()}, at)
        order = prepared.evolved(
            status=RebookStatus.SETTLED.value,
            verdict=RebookVerdict.RECALLED.value,
            reason="cancel_accepted",
            at=at,
        )
        session.state.rebook_orders[order.id] = order
        session.record("rebook.put", {"order": order.as_dict()}, at)
        session.record("counter.add", {"name": "rebook.recalled", "delta": 1}, at)
        return order

    def _finish_let_ride(
        self,
        session,
        prepared: RebookOrder,
        landed_chute_id: str,
        ride_completed: bool,
        at: str | None = None,
    ) -> RebookOrder:
        """Cancel rejected or bag past the point: keep the old mapping on the ride."""

        moment = at or self._clock.now_iso()
        pending = session.state.targets.get(prepared.target_id)
        if pending is None:
            waiting = [
                target
                for target in self._targets.for_bag(prepared.bag_id)
                if target.status == TargetStatus.PENDING.value
                and target.flight_id == prepared.new_flight_id
            ]
            pending = waiting[-1] if waiting else None
        if ride_completed and pending is not None:
            old = self._targets.active(prepared.bag_id)
            if old is not None:
                self._targets.settle(session, old, TargetStatus.DELIVERED)
            self._targets.activate(session, prepared.bag_id, pending)
            self._enqueue_manual_transfer(session, prepared, landed_chute_id, moment)
        order = prepared.evolved(
            status=RebookStatus.SETTLED.value,
            verdict=RebookVerdict.LET_RIDE.value,
            target_id=pending.id if (ride_completed and pending is not None) else prepared.target_id,
            landed_chute_id=landed_chute_id if ride_completed else "",
            reason="ride_completed" if ride_completed else prepared.reason or "cancel_rejected",
            at=moment,
        )
        session.state.rebook_orders[order.id] = order
        session.record("rebook.put", {"order": order.as_dict()}, moment)
        session.record("counter.add", {"name": "rebook.let_ride", "delta": 1}, moment)
        return order

    def _finish_let_ride_entry(
        self, order: RebookOrder, landed_chute_id: str, ride_completed: bool
    ) -> RebookOrder:
        at = self._clock.now_iso()
        with self._store.session() as session:
            return self._finish_let_ride(session, order, landed_chute_id, ride_completed, at)

    def _enqueue_manual_transfer(self, session, order: RebookOrder, chute_id: str, at: str) -> None:
        """The let-ride bag becomes recheck/manual-transfer work from the old chute."""

        bag = session.state.bags[order.bag_id]
        if bag.state is not BagState.DIVERTED:
            updated = bag.evolved(state=BagState.DIVERTED)
            session.state.bags[order.bag_id] = updated
            session.record("bag.put", {"bag": updated.as_dict()}, at)
        if order.bag_id not in session.state.recheck_queue:
            session.state.recheck_queue.append(order.bag_id)
            session.record("recheck.enqueue", {"bag_id": order.bag_id}, at)

    # -- branch: let ride on the loop (ride not complete yet) -------------

    def _prepare_let_ride(self, decision: RerouteDecision, request_id: str) -> RebookOrder:
        """Keep the old target active for the ride; stage the new one pending."""

        at = self._clock.now_iso()
        with self._store.session() as session:
            self._targets.create(
                session,
                decision.bag_id,
                decision.new_flight_id,
                decision.new_chute_id,
                TargetStatus.PENDING,
                request_id,
                at,
            )
            order = self._record_order(
                session,
                decision,
                request_id,
                RebookStatus.PREPARED,
                RebookVerdict.LET_RIDE,
                target_id="",
                at=at,
            )
            session.record(
                "counter.add", {"name": "rebook.let_ride_staged", "delta": 1}, at
            )
        return order

    # -- helpers ----------------------------------------------------------

    def _assert_open_rebook(self, bag_id: str, new_flight_id: str) -> None:
        state = self._store.state()
        if bag_id not in state.bags:
            raise NotFoundError(f"bag {bag_id!r} is not registered")
        if new_flight_id not in state.flights:
            raise NotFoundError(f"flight {new_flight_id!r} is not registered")
        self._book.require_open(new_flight_id)
        if self._targets.active(bag_id) is None:
            raise ConflictError(f"bag {bag_id!r} has no active target to rebook")
        duplicate = self.orders_for(bag_id)
        for order in duplicate:
            if (
                order.new_flight_id == new_flight_id
                and order.status == RebookStatus.SETTLED.value
            ):
                raise ConflictError(
                    f"bag {bag_id!r} already has a settled rebook to {new_flight_id!r}"
                )

    def _record_order(
        self,
        session,
        decision: RerouteDecision,
        request_id: str,
        status: RebookStatus,
        verdict: RebookVerdict,
        target_id: str = "",
        command_id: str = "",
        at: str = "",
    ) -> RebookOrder:
        number = len(session.state.rebook_orders) + 1
        order = RebookOrder(
            id=f"rbk-{decision.bag_id}-{number}",
            request_id=request_id,
            bag_id=decision.bag_id,
            old_flight_id=decision.old_flight_id,
            new_flight_id=decision.new_flight_id,
            status=status.value,
            verdict=verdict.value,
            phase=decision.phase.value,
            position=decision.position,
            recall_point=decision.feasibility.recall_point if decision.feasibility else 0,
            divert_at=decision.feasibility.divert_at if decision.feasibility else 0,
            target_id=target_id,
            command_id=command_id,
            landed_chute_id="",
            reason=decision.feasibility.reason if decision.feasibility else "",
            at=at or self._clock.now_iso(),
        )
        session.state.rebook_orders[order.id] = order
        session.record("rebook.put", {"order": order.as_dict()}, order.at)
        session.state.rebook_requests[request_id] = order.id
        session.record(
            "rebook.index.put", {"request_id": request_id, "order_id": order.id}, order.at
        )
        return order
