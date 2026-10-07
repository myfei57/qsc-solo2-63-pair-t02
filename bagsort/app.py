"""Assembly of the handling line.

Every subsystem is constructed here and every operator visible action is a
method on :class:`Plant`, which is what keeps the console handlers thin and
lets the CLI drive the same operations the HTTP API exposes.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.audit.chain import AuditChain
from bagsort.belt.belt import Belt
from bagsort.checkin.desk import Desk
from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.events.bus import EventBus
from bagsort.flight.book import FlightBook
from bagsort.flight.close import FlightCloser
from bagsort.flight.mapping import BagMapping
from bagsort.flight.schedule import ScheduleBoard
from bagsort.ids import IdFactory
from bagsort.inject.injector import Injector
from bagsort.model.entities import Bag, Flight, Reading, SortRecord
from bagsort.model.errors import unknown_bag
from bagsort.ns.chutes import ChuteRegistry
from bagsort.ns.namespace import Namespace
from bagsort.ns.topology import handling_line
from bagsort.quota.quota import QuotaBook
from bagsort.recheck.queue import RecheckQueue
from bagsort.reporting.report import PlantReport
from bagsort.sequence.verdict import VerdictTracker
from bagsort.sorter.sorter import Sorter
from bagsort.store.store import Store
from bagsort.tag.history import ReadHistory
from bagsort.tag.reader import TagReader
from bagsort.chute.assigner import ChuteAssigner
from bagsort.chute.history import AssignmentHistory
from bagsort.console.metrics import Metrics
from bagsort.console.telemetry import Telemetry
from bagsort.line.controller import DivertController
from bagsort.line.enums import TargetStatus
from bagsort.line.gateway import LineGateway
from bagsort.line.geometry import LineGeometry
from bagsort.line.targets import TargetBoard
from bagsort.line.tracker import LineTracker
from bagsort.rebook.planner import ReroutePlanner
from bagsort.rebook.policy import PolicyBook
from bagsort.rebook.reconcile import Reconciler
from bagsort.rebook.service import RebookResult, RebookService

DEFAULT_TERMINAL = "T1"


@dataclass
class Plant:
    """The wired plant plus the operator level operations the console calls."""

    store: Store
    clock: Clock
    namespace: Namespace
    registry: ChuteRegistry
    bus: EventBus
    telemetry: Telemetry
    metrics: Metrics
    reader: TagReader
    reads: ReadHistory
    book: FlightBook
    mapping: BagMapping
    schedule: ScheduleBoard
    closer: FlightCloser
    chutes: ChuteAssigner
    chute_history: AssignmentHistory
    sorter: Sorter
    injector: Injector
    desk: Desk
    recheck: RecheckQueue
    belt: Belt
    quota: QuotaBook
    audit: AuditChain
    report: PlantReport
    geometry: LineGeometry
    gateway: LineGateway
    targets: TargetBoard
    controller: DivertController
    tracker: LineTracker
    policies: PolicyBook
    planner: ReroutePlanner
    rebooks: RebookService
    reconciler: Reconciler

    @property
    def topology(self):
        return handling_line()

    # -- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        store: Store,
        terminal: str = DEFAULT_TERMINAL,
        clock: Clock | None = None,
    ) -> "Plant":
        clock = clock or Clock()
        namespace = Namespace(terminal)
        registry = ChuteRegistry()
        bus = EventBus()
        telemetry = Telemetry()
        metrics = Metrics()
        bus.subscribe("*", lambda event: telemetry.observe(event.topic))

        reader = TagReader(store, namespace, clock)
        reads = ReadHistory(store)
        book = FlightBook(store, clock)
        mapping = BagMapping(store, clock)
        schedule = ScheduleBoard(store, clock)
        chutes = ChuteAssigner(store, clock)
        chute_history = AssignmentHistory(store)
        sorter = Sorter(store, mapping, chutes, clock, VerdictTracker())
        injector = Injector(store, book, sorter, clock)
        quota = QuotaBook(store, clock)
        desk = Desk(store, reader, book, mapping, injector, quota, clock, handling_line())
        closer = FlightCloser(book)
        recheck = RecheckQueue(store, book, clock)
        belt = Belt(store, clock, IdFactory("cmd"))

        audit = AuditChain(store, clock, handling_line())
        audit_subscriber = _AuditSubscriber(audit)
        for topic in (
            "bag.checked_in",
            "bag.sorted",
            "bag.diverted",
            "bag.recheck_enqueued",
            "flight.closed",
            "chute.assigned",
            "belt.switched",
            "bag.remapped",
            "rebook.induction_swap",
            "rebook.recalled",
            "rebook.let_ride",
            "rebook.policy_sealed",
        ):
            bus.subscribe(topic, audit_subscriber)

        geometry = LineGeometry()
        gateway = LineGateway()
        targets = TargetBoard(store, clock)
        controller = DivertController(store, clock)
        tracker = LineTracker(store, geometry, controller, gateway, clock, sorter.log)
        policies = PolicyBook(store, clock)
        planner = ReroutePlanner(store, tracker, targets, chutes, geometry)
        rebooks = RebookService(
            store,
            clock,
            book,
            planner,
            targets,
            controller,
            tracker,
            gateway,
            policies,
            recheck,
        )
        reconciler = Reconciler(store)

        report = PlantReport(
            store, book, chutes, belt, quota, audit, namespace, handling_line()
        )
        return cls(
            store=store,
            clock=clock,
            namespace=namespace,
            registry=registry,
            bus=bus,
            telemetry=telemetry,
            metrics=metrics,
            reader=reader,
            reads=reads,
            book=book,
            mapping=mapping,
            schedule=schedule,
            closer=closer,
            chutes=chutes,
            chute_history=chute_history,
            sorter=sorter,
            injector=injector,
            desk=desk,
            recheck=recheck,
            belt=belt,
            quota=quota,
            audit=audit,
            report=report,
            geometry=geometry,
            gateway=gateway,
            targets=targets,
            controller=controller,
            tracker=tracker,
            policies=policies,
            planner=planner,
            rebooks=rebooks,
            reconciler=reconciler,
        )

    # -- operator operations ----------------------------------------------

    def register_flight(self, code: str, carousel: str = "", chute: str = "") -> Flight:
        flight = Flight(
            id=self.namespace.flight_id(code),
            code=code.strip().upper(),
            terminal=self.namespace.terminal,
            carousel=self.namespace.carousel_id(carousel) if carousel else "",
        )
        self.book.register(flight)
        if chute:
            self.assign_chute(flight.id, self.namespace.chute_id(chute))
        return self.book.get(flight.id)

    def assign_chute(self, flight_id: str, chute_id: str):
        assignment = self.chutes.assign(flight_id, chute_id)
        self.bus.publish("chute.assigned", flight_id=flight_id, chute_id=chute_id)
        return assignment

    def read_barcode(self, barcode: str) -> Reading:
        return self.reader.read(barcode)

    def check_in(self, barcode: str, flight_id: str) -> Bag:
        bag = self.desk.check_in(barcode, flight_id)
        self.bus.publish("bag.checked_in", bag_id=bag.id, flight_id=flight_id)
        self._record_initial_target(bag.id, flight_id)
        return bag

    def _record_initial_target(self, bag_id: str, flight_id: str) -> None:
        """Version the original target so every later target has a predecessor."""

        assignment = self.chutes.current(flight_id)
        with self.store.session() as session:
            self.targets.create(
                session,
                bag_id,
                flight_id,
                assignment.chute_id if assignment else "",
                TargetStatus.ACTIVE,
            )

    def cancel_check_in(self, bag_id: str) -> Bag:
        bag = self.desk.cancel(bag_id)
        self.bus.publish("bag.diverted", bag_id=bag.id, reason="checkin_cancelled")
        with self.store.session() as session:
            self.targets.deactivate(session, bag.id, TargetStatus.DELIVERED)
        return bag

    def bag(self, bag_id: str) -> Bag:
        bag = self.store.state().bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        return bag

    def push_bag(self, bag_id: str, chute_id: str) -> SortRecord:
        bag = self.bag(bag_id)
        record = self.injector.push(bag, chute_id)
        self.bus.publish("bag.sorted", bag_id=bag.id, chute_id=record.chute_id)
        return record

    def retry_bag(self, bag_id: str) -> bool:
        bag = self.bag(bag_id)
        sorted_now = self.injector.retry(bag)
        if sorted_now:
            self.bus.publish("bag.sorted", bag_id=bag.id, retry=True)
        return sorted_now

    def divert_bag(self, bag_id: str) -> Bag:
        bag = self.bag(bag_id)
        updated = self.sorter.divert(bag)
        self.bus.publish("bag.diverted", bag_id=bag.id)
        return updated

    def remap_bag(self, bag_id: str, flight_id: str) -> Bag:
        self.book.require_open(flight_id)
        self.mapping.remap(bag_id, flight_id)
        self.bus.publish("bag.remapped", bag_id=bag_id, flight_id=flight_id)
        return self.bag(bag_id)

    # -- sortation loop and rebooks ---------------------------------------

    def enter_line(self, bag_id: str) -> dict[str, object]:
        """Move a bag from the induction desk onto the sortation loop."""

        bag = self.bag(bag_id)
        assignment = self.chutes.require(self.mapping.flight_of(bag_id))
        tracking = self.tracker.enter(bag, assignment.chute_id)
        return tracking.as_dict()

    def observe_bag(self, bag_id: str, position: int) -> dict[str, object]:
        landed = self.tracker.observe(bag_id, position, self._settle_landed)
        for landed_id in landed:
            self.bus.publish(
                "bag.sorted", bag_id=landed_id, chute_id=self.bag(landed_id).chute_id
            )
            if landed_id != bag_id:
                self._settle_landed(landed_id)
        self._settle_landed(bag_id)
        tracking = self.tracker.tracking(bag_id)
        return tracking.as_dict() if tracking else {"bag_id": bag_id, "phase": "landed"}

    def advance_line(self, cells: int = 1) -> list[str]:
        """Tick the loop; any let-ride order whose bag lands is settled."""

        landed = self.tracker.tick(cells)
        for bag_id in landed:
            self.bus.publish("bag.sorted", bag_id=bag_id, chute_id=self.bag(bag_id).chute_id)
            self._settle_landed(bag_id)
        return landed

    def set_chute_cell(self, chute_id: str, cell: int) -> None:
        self.geometry.set_chute_cell(chute_id, cell)

    def set_gateway_strict(self, bag_id: str, reject: str) -> None:
        """Fault injection: force the next arm or cancel for a bag to NACK."""

        self.gateway.set_strict(bag_id, reject)

    def reroute_preview(self, bag_id: str, flight_id: str) -> dict[str, object]:
        """The decision the rebook would make, without applying it."""

        return self.planner.evaluate(bag_id, flight_id).as_dict()

    def rebook_bag(self, bag_id: str, flight_id: str, request_id: str) -> RebookResult:
        """Apply a passenger rebook for one bag and publish its conclusion."""

        result = self.rebooks.rebook(bag_id, flight_id, request_id)
        self._publish_rebook(result)
        return result

    def settle_rebook(self, order_id: str) -> RebookResult:
        """Crash/retry recovery: finish an order left in the prepared state."""

        result = self.rebooks.settle_prepared(order_id)
        self._publish_rebook(result)
        return result

    def _settle_landed(self, bag_id: str) -> None:
        order = self.rebooks.settle_landing(bag_id)
        if order is not None:
            self.bus.publish(
                "bag.recheck_enqueued",
                bag_id=bag_id,
                reason="manual_transfer_after_let_ride",
            )
            self.bus.publish(
                "rebook.let_ride",
                bag_id=bag_id,
                old_flight_id=order.old_flight_id,
                new_flight_id=order.new_flight_id,
                request_id=order.request_id,
                verdict=order.verdict,
                landed_chute_id=order.landed_chute_id,
                reason=order.reason,
            )

    def _publish_rebook(self, result: RebookResult) -> None:
        order = result.order
        if result.policy_sealed:
            policy = self.policies.current()
            self.bus.publish(
                "rebook.policy_sealed",
                flight_id=order.new_flight_id,
                policy=policy.name if policy else "",
                rejected=policy.rejected if policy else "",
            )
        if order.status != "settled":
            return
        topic = {
            "swapped_at_induction": "rebook.induction_swap",
            "recalled": "rebook.recalled",
            "let_ride": "rebook.let_ride",
        }.get(order.verdict)
        if topic is None:
            return
        self.bus.publish(
            topic,
            bag_id=order.bag_id,
            old_flight_id=order.old_flight_id,
            new_flight_id=order.new_flight_id,
            request_id=order.request_id,
            verdict=order.verdict,
            landed_chute_id=order.landed_chute_id,
            reason=order.reason,
            position=order.position,
            recall_point=order.recall_point,
            divert_at=order.divert_at,
        )

    def rebook_policy(self) -> dict[str, object] | None:
        policy = self.policies.current()
        return policy.as_dict() if policy else None

    def rebook_integrity(self) -> dict[str, object]:
        return self.reconciler.integrity_report()

    def rebook_reconcile(self, order_id: str) -> dict[str, object]:
        return self.reconciler.reconcile_order(order_id).as_dict()

    def rebook_reconcile_all(self) -> list[dict[str, object]]:
        return [item.as_dict() for item in self.reconciler.reconcile_all()]


    def revise_schedule(self, flight_id: str, cutoff_at: str):
        return self.schedule.revise(flight_id, cutoff_at)

    def close_flight(self, flight_id: str) -> Flight:
        flight = self.closer.close(flight_id, self.injector)
        self.bus.publish("flight.closed", flight_id=flight_id)
        return flight

    def enqueue_recheck(self, bag_id: str) -> Bag:
        bag = self.bag(bag_id)
        self.recheck.enqueue(bag)
        self.bus.publish("bag.recheck_enqueued", bag_id=bag_id)
        return bag

    def process_recheck(self) -> Bag | None:
        return self.recheck.dequeue()

    def switch_belt(self, carousel: str, batch: int) -> int:
        cleared = self.belt.switch(carousel, batch)
        self.bus.publish("belt.switched", carousel=carousel, batch=batch)
        return cleared

    def rotate_belt(self) -> int:
        return self.belt.rotate()

    def append_transfer(self, flight_id: str, carousel: str, batch: int):
        return self.belt.append_transfer(flight_id, carousel, batch)

    def set_quota(self, flight_id: str, limit: int) -> int:
        return self.quota.set_limit(flight_id, limit)

    def verify(self) -> bool:
        return self.audit.verify()


class _AuditSubscriber:
    """Turns published operations into linked audit entries."""

    _STAGE = {
        "bag.checked_in": ("checkin", "checkin"),
        "bag.sorted": ("sort", "sorter"),
        "bag.diverted": ("diversion", "sorter"),
        "bag.recheck_enqueued": ("recheck", "recheck"),
        "flight.closed": ("flight_close", "audit"),
        "chute.assigned": ("chute_assign", "chute"),
        "belt.switched": ("belt_switch", "belt"),
        "bag.remapped": ("remap", "checkin"),
        "rebook.induction_swap": ("rebook_induction_swap", "checkin"),
        "rebook.recalled": ("rebook_recall", "sorter"),
        "rebook.let_ride": ("rebook_let_ride", "chute"),
        "rebook.policy_sealed": ("rebook_policy", "audit"),
    }

    def __init__(self, audit: AuditChain) -> None:
        self._audit = audit

    def __call__(self, event) -> None:
        mapping = self._STAGE.get(event.topic)
        if mapping is None:
            raise ConflictError(f"no audit mapping for topic {event.topic!r}")
        kind, stage = mapping
        subject = str(
            event.payload.get("bag_id")
            or event.payload.get("flight_id")
            or event.payload.get("carousel")
            or ""
        )
        detail = {
            key: value
            for key, value in event.payload.items()
            if key not in {"bag_id", "flight_id"}
        }
        self._audit.record(kind, subject, stage, detail)
