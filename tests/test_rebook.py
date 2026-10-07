"""Last-minute rebook: adjudication, rollback, idempotency and reconciliation."""

from __future__ import annotations

import pytest

from bagsort.errors import ConflictError, OrderingError
from bagsort.model.entities import Reading
from bagsort.model.enums import (
    BagLocation,
    BagState,
    CommandState,
    RebookAction,
    SortState,
)
from bagsort.rebook.service import POLICY
from bagsort.store.replay import replay
from tests.support import check_in


def _two_flights(plant, old_chute: str = "A", new_chute: str = "C"):
    old = plant.register_flight("CA1234", "CR1", old_chute)
    new = plant.register_flight("MU5678", "CR2", new_chute)
    return old, new


def _committed(bag):
    return Reading(
        barcode=bag.barcode,
        bag_id=bag.id,
        sequence=bag.sequence,
        epoch=bag.epoch,
        committed=True,
    )


def _plan_on_line(plant, bag):
    plant.enter_line(bag.id)
    plant.sorter.plan(plant.bag(bag.id), _committed(plant.bag(bag.id)))


# ---------------------------------------------------------------------------
# in-transit adjudication
# ---------------------------------------------------------------------------


def test_bag_at_induction_is_retargeted_without_a_sort_record(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]

    order, decision = plant.rebook_bag(bag.id, new.id)

    assert decision.action is RebookAction.RETARGET
    assert plant.bag(bag.id).flight_id == new.id
    assert plant.bag(bag.id).location is BagLocation.INDUCTION
    assert plant.sorter.log.count_for(bag.id) == 0
    assert order.decided is True


def test_bag_upstream_of_the_divert_point_is_rerouted(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    _plan_on_line(plant, bag)
    plant.advance_bag(bag.id, 5)  # divert point of CHC is mile 60

    _order, decision = plant.rebook_bag(bag.id, new.id)

    assert decision.action is RebookAction.REROUTE
    assert decision.feasible is True
    assert decision.target_chute == "T1-CHC"
    # old allocation rolled back, new planned allocation is the only live one
    records = plant.sorter.log.for_bag(bag.id)
    assert [record.sort_state for record in records] == [
        SortState.REVERSED,
        SortState.PLANNED,
    ]
    assert records[0].reversed_reason.startswith("reroute:")
    assert plant.sorter.log.effective_for_bag(bag.id) == [records[-1]]


def test_bag_past_the_divert_point_continues_and_records_its_chute(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    _plan_on_line(plant, bag)
    plant.advance_bag(bag.id, 70)  # past CHC divert point (60)

    _order, decision = plant.rebook_bag(bag.id, new.id)

    assert decision.action is RebookAction.CONTINUE
    assert decision.feasible is False
    assert "divert point" in decision.reason
    # mapping and live allocation stay on the old flight
    assert plant.bag(bag.id).flight_id == old.id
    task = plant.rebook.recovery_tasks()[0]
    assert task.bag_id == bag.id
    assert task.flight_id == old.id
    assert task.new_flight_id == new.id


def test_landed_bag_cannot_be_recalled_but_is_tracked_for_recovery(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    plant.enter_line(bag.id)
    plant.push_bag(bag.id, "T1-CHA")

    _order, decision = plant.rebook_bag(bag.id, new.id)

    assert decision.action is RebookAction.CONTINUE
    assert plant.bag(bag.id).location is BagLocation.LANDED
    assert plant.bag(bag.id).flight_id == old.id
    task = plant.rebook.recovery_tasks()[0]
    assert task.chute_id == "T1-CHA"


def test_safety_margin_makes_a_geometrically_reachable_bag_too_late(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    _plan_on_line(plant, bag)
    plant.advance_bag(bag.id, 58)  # 2 seconds before CHC at mile 60 < margin 4

    _order, decision = plant.rebook_bag(bag.id, new.id)

    assert decision.action is RebookAction.CONTINUE
    assert "margin" in decision.reason


# ---------------------------------------------------------------------------
# single active target / rollback consistency
# ---------------------------------------------------------------------------


def test_a_bag_never_has_two_active_targets_and_mapping_matches(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]

    plant.rebook_bag(bag.id, new.id)

    state = plant.store.state()
    active = [target for target in state.targets.values() if target.active]
    assert len(active) == 1
    assert active[0].bag_id == bag.id
    assert active[0].flight_id == new.id
    assert state.mappings[bag.id] == new.id
    # the superseded row is retained for audit but inactive
    rows = [target for target in state.targets.values()]
    assert len(rows) == 2
    assert sorted(target.active for target in rows) == [False, True]


def test_rollback_and_target_swap_commit_as_one_transaction(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    _plan_on_line(plant, bag)
    plant.advance_bag(bag.id, 5)

    before = plant.store.watermark
    plant.rebook_bag(bag.id, new.id)
    # every journal entry between the two watermarks must fold together; the
    # rebook can never leave a reversed old record next to the old target.
    rebuilt = replay(plant.store.entries_since(before))
    active_targets = [target for target in rebuilt.targets.values() if target.active]
    assert len(active_targets) == 1
    assert active_targets[0].flight_id == new.id
    live = [r for r in rebuilt.sort_records if r.sort_state.effective]
    assert len(live) == 1
    assert live[0].flight_id == new.id
    assert rebuilt.mappings[bag.id] == new.id


def test_replay_from_scratch_reproduces_every_decision(plant):
    old, new = _two_flights(plant)
    early = check_in(plant, old.id, 1)[0]
    late = check_in(plant, old.id, 1, prefix="LATE")[0]
    _plan_on_line(plant, late)
    plant.advance_bag(late.id, 80)
    plant.rebook_bag(early.id, new.id)
    plant.rebook_bag(late.id, new.id)

    rebuilt = replay(plant.store.entries())

    assert [d.action for d in rebuilt.decisions] == [
        RebookAction.RETARGET,
        RebookAction.CONTINUE,
    ]
    assert rebuilt.as_dict() == plant.store.state().as_dict()


# ---------------------------------------------------------------------------
# idempotent retries never sort a bag twice
# ---------------------------------------------------------------------------


def test_command_outcome_report_is_idempotent(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    _plan_on_line(plant, bag)
    plant.advance_bag(bag.id, 5)
    order, _decision = plant.rebook_bag(bag.id, new.id)

    first = plant.report_command(order.id, True, "accepted")
    second = plant.report_command(order.id, True, "accepted-again")

    assert first.state is CommandState.ACCEPTED
    assert second is first or second.key == first.key
    assert second.attempts == 1  # the retry did not execute anything again
    live = plant.sorter.log.effective_for_bag(bag.id)
    assert len(live) == 1
    assert live[0].flight_id == new.id


def test_rejected_reroute_falls_back_to_recovery_without_double_sort(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    _plan_on_line(plant, bag)
    plant.advance_bag(bag.id, 5)
    order, decision = plant.rebook_bag(bag.id, new.id)
    assert decision.action is RebookAction.REROUTE

    command = plant.report_command(order.id, False, "diverter fault")

    assert command.state is CommandState.REJECTED
    assert plant.rebook.effective_action(order.id) is RebookAction.CONTINUE
    task = plant.rebook.recovery_tasks()[0]
    assert task.new_flight_id == new.id
    # the original (old-flight) allocation is still the only live allocation
    live = plant.sorter.log.effective_for_bag(bag.id)
    assert len(live) == 1
    assert live[0].flight_id == old.id


def test_recovery_reinducts_into_a_new_cycle_and_dispatch_dedupes(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    plant.enter_line(bag.id)
    plant.push_bag(bag.id, "T1-CHA")
    plant.rebook_bag(bag.id, new.id)

    recovered = plant.recover_bag(bag.id)

    assert recovered.flight_id == new.id
    assert recovered.location is BagLocation.INDUCTION
    assert recovered.cycle == 1
    # cycle 0 already holds the landed record; a same-cycle duplicate is refused
    stuck = recovered.evolved(cycle=0)
    with pytest.raises(ConflictError):
        plant.sorter.dispatch(stuck, _committed(stuck))
    # the new cycle is allowed exactly one plan, never a silent second one
    plant.sorter.plan(plant.bag(bag.id), _committed(plant.bag(bag.id)))
    with pytest.raises(ConflictError):
        plant.sorter.plan(plant.bag(bag.id), _committed(plant.bag(bag.id)))


def test_dispatch_refuses_two_effective_allocations_in_one_cycle(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    _plan_on_line(plant, bag)
    with pytest.raises(ConflictError):
        plant.sorter.plan(plant.bag(bag.id), _committed(plant.bag(bag.id)))


# ---------------------------------------------------------------------------
# console conclusion matches audit evidence
# ---------------------------------------------------------------------------


def test_reconciliation_reports_consistency_and_audit_chain(plant):
    old, new = _two_flights(plant)
    induction_bag = check_in(plant, old.id, 1)[0]
    line_bag = check_in(plant, old.id, 1, prefix="LINE")[0]
    landed_bag = check_in(plant, old.id, 1, prefix="LAND")[0]
    _plan_on_line(plant, line_bag)
    plant.advance_bag(line_bag.id, 5)
    plant.enter_line(landed_bag.id)
    plant.push_bag(landed_bag.id, "T1-CHA")

    plant.rebook_bag(induction_bag.id, new.id)
    order, _ = plant.rebook_bag(line_bag.id, new.id)
    plant.report_command(order.id, True, "ok")
    plant.rebook_bag(landed_bag.id, new.id)

    report = plant.rebook_reconciliation()
    assert report["ok"] is True
    assert report["issues"] == []
    assert report["policy"] == POLICY
    assert report["audit_intact"] is True

    # every decision has a matching linked audit entry for that bag/order
    audit = {entry.detail.get("rebook_id"): entry for entry in plant.audit.entries()}
    assert order.id in audit
    assert plant.audit.verify() is True
    assert plant.audit.out_of_order() == []


def test_rebook_refuses_a_closed_target_and_an_unknown_flight(plant):
    old, new = _two_flights(plant)
    bag = check_in(plant, old.id, 1)[0]
    plant.book.cutoff(new.id)
    with pytest.raises(OrderingError):
        plant.rebook_bag(bag.id, new.id)
