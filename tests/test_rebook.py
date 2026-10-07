"""Passenger rebook: in-transit judgement, reroute, recall and reconciliation."""

from __future__ import annotations

import json

import pytest

from bagsort.errors import ConflictError, OrderingError
from bagsort.line.enums import LinePhase, RebookVerdict
from bagsort.store.replay import replay
from tests.support import register_flight


def _two_flights(plant, code_a="CA1", code_b="MU2"):
    first = register_flight(plant, code=code_a, carousel="CR1", chute="A")
    second = register_flight(plant, code=code_b, carousel="CR1", chute="B")
    plant.assign_chute(first.id, "T1-CHA")
    plant.assign_chute(second.id, "T1-CHB")
    return first, second


def _line_bag(plant, flight, barcode="7001"):
    bag = plant.check_in(barcode, flight.id)
    plant.enter_line(bag.id)
    return bag


def test_bag_at_induction_swaps_flight_directly(plant):
    old, new = _two_flights(plant)
    bag = plant.check_in("7001", old.id)
    result = plant.rebook_bag(bag.id, new.id, "req-1")
    assert result.final
    assert result.order.verdict == RebookVerdict.SWAPPED_AT_INDUCTION.value
    assert plant.mapping.flight_of(bag.id) == new.id
    assert plant.bag(bag.id).flight_id == new.id
    assert plant.tracker.phase_of(bag.id) is LinePhase.NOT_ENTERED
    assert len(plant.sorter.records()) == 0
    assert plant.rebook_policy()["name"] == "commit_point_cutoff"
    assert plant.rebook_policy()["rejected"] == "recall_all_then_reroute"


def test_bag_before_recall_point_is_recalled_and_sort_plan_rolled_back(plant):
    old, new = _two_flights(plant)
    bag = _line_bag(plant, old)
    plant.advance_line(8)  # recall point is chute cell 10 minus margin 2
    result = plant.rebook_bag(bag.id, new.id, "req-2")
    assert result.order.verdict == RebookVerdict.RECALLED.value
    # the armed allocation is rolled back, nothing physically happened
    records = plant.sorter.records()
    assert [(r.status, r.effective) for r in records] == [("rolled_back", False)]
    assert plant.mapping.flight_of(bag.id) == new.id
    assert plant.tracker.phase_of(bag.id) is LinePhase.RECALLED
    assert plant.sorter.log.was_executed(bag.id) is False
    # the recalled bag re-enters towards the new chute and is sorted exactly once
    plant.enter_line(bag.id)
    plant.advance_line(25)
    assert plant.bag(bag.id).chute_id == "T1-CHB"
    assert plant.sorter.log.effective_count_for(bag.id) == 1


def test_bag_past_recall_point_rides_on_and_is_settled_at_landing(plant):
    old, new = _two_flights(plant)
    bag = _line_bag(plant, old)
    plant.advance_line(9)  # one cell beyond the last recallable cell
    result = plant.rebook_bag(bag.id, new.id, "req-3")
    assert result.order.verdict == RebookVerdict.LET_RIDE.value
    assert result.final is False
    # while riding, the old target stays the only effective one
    assert plant.mapping.flight_of(bag.id) == old.id
    assert len([t for t in plant.targets.for_bag(bag.id) if t.status == "active"]) == 1
    plant.advance_line(3)  # lands in the old chute
    order = plant.rebooks.orders_for(bag.id)[-1]
    assert order.landed_chute_id == "T1-CHA"
    assert order.status == "settled"
    assert plant.mapping.flight_of(bag.id) == new.id
    assert bag.id in plant.store.state().recheck_queue  # manual transfer work


def test_cancel_nack_keeps_the_old_divert_and_still_finishes_once(plant):
    old, new = _two_flights(plant)
    bag = _line_bag(plant, old)
    plant.gateway.set_strict(bag.id, "cancel")  # arm succeeds; only the recall NACKs
    result = plant.rebook_bag(bag.id, new.id, "req-4")
    assert result.order.verdict == RebookVerdict.LET_RIDE.value
    assert result.final is False
    commands = plant.controller.for_bag(bag.id)
    assert commands[0].state == "armed"
    assert plant.sorter.log.pending_for(bag.id) is not None
    plant.advance_line(12)
    order = plant.rebooks.orders_for(bag.id)[-1]
    assert order.landed_chute_id == "T1-CHA"
    assert plant.sorter.log.effective_count_for(bag.id) == 1


def test_duplicate_request_id_is_idempotent(plant):
    old, new = _two_flights(plant)
    bag = plant.check_in("7001", old.id)
    first = plant.rebook_bag(bag.id, new.id, "same")
    second = plant.rebook_bag(bag.id, new.id, "same")
    assert first.order.id == second.order.id
    assert len(plant.rebooks.orders_for(bag.id)) == 1
    assert len(plant.targets.for_bag(bag.id)) == 2


def test_a_second_settled_rebook_to_the_same_flight_is_refused(plant):
    old, new = _two_flights(plant)
    bag = plant.check_in("7001", old.id)
    plant.rebook_bag(bag.id, new.id, "req-1")
    with pytest.raises(ConflictError):
        plant.rebook_bag(bag.id, new.id, "req-2")


def test_rebook_requires_a_request_id(plant):
    old, new = _two_flights(plant)
    bag = plant.check_in("7001", old.id)
    with pytest.raises(OrderingError):
        plant.rebooks.rebook(bag.id, new.id, "  ")


def test_prepared_recall_is_recovered_after_a_crash(plant):
    old, new = _two_flights(plant)
    bag = _line_bag(plant, old)
    decision = plant.planner.evaluate(bag.id, new.id)
    prepared = plant.rebooks._prepare_recall(decision, "crash-1")
    assert prepared.status == "prepared"
    assert plant.mapping.flight_of(bag.id) == old.id  # mapping unchanged mid-flight
    recovered = plant.settle_rebook(prepared.id)
    assert recovered.final
    assert recovered.order.verdict == RebookVerdict.RECALLED.value
    # redriving recovery is a no-op
    again = plant.settle_rebook(prepared.id)
    assert again.order.id == prepared.id
    assert len(plant.rebooks.orders_for(bag.id)) == 1


def test_integrity_and_reconciliation_hold_across_every_branch(plant):
    old, new = _two_flights(plant)
    # induction
    bag_a = plant.check_in("7001", old.id)
    plant.rebook_bag(bag_a.id, new.id, "req-a")
    # recall
    bag_b = _line_bag(plant, old, barcode="7002")
    plant.rebook_bag(bag_b.id, new.id, "req-b")
    # let ride
    bag_c = _line_bag(plant, old, barcode="7003")
    plant.advance_line(9)
    plant.rebook_bag(bag_c.id, new.id, "req-c")
    plant.advance_line(3)

    integrity = plant.rebook_integrity()
    assert integrity["holds"] is True, integrity["violations"]
    assert integrity["audit_intact"] is True
    for item in plant.rebook_reconcile_all():
        assert item["matches"] is True, item
    assert plant.audit.verify()


def test_replay_rebuilds_every_rebook_artefact(plant):
    old, new = _two_flights(plant)
    bag = _line_bag(plant, old)
    plant.advance_line(9)
    plant.rebook_bag(bag.id, new.id, "req-r")
    plant.advance_line(3)
    rebuilt = replay(plant.store.entries()).as_dict()
    assert rebuilt == plant.store.state().as_dict()


def test_rebook_towards_a_chute_already_passed_lets_the_bag_ride(plant):
    old, new = _two_flights(plant)
    bag = _line_bag(plant, old)
    plant.advance_line(25)  # past the A diverter (10) but not B (20)... it lands at A
    # once landed at A, any rebook can only be manual transfer
    result = plant.rebook_bag(bag.id, new.id, "req-late")
    assert result.order.verdict == RebookVerdict.LET_RIDE.value
    assert plant.bag(bag.id).chute_id == "T1-CHA"


def test_scanner_observe_advances_the_belt_not_just_one_bag(plant):
    old, new = _two_flights(plant)
    first = _line_bag(plant, old)
    second = plant.check_in("7002", old.id)
    plant.enter_line(second.id)
    plant.observe_bag(first.id, 5)
    assert plant.tracker.position_of(first.id) == 5
    assert plant.tracker.position_of(second.id) == 5
    assert plant.rebook_integrity()["holds"]


def test_console_conclusion_matches_the_audit_trail(server):
    def call(method, path, payload=None, query=None):
        response = server.call(method, path, payload, query)
        return response.status, json.loads(response.body)

    status, flight_a = call("POST", "/api/flights", {"code": "CA1", "carousel": "CR1", "chute": "A"})
    assert status == 201
    status, flight_b = call("POST", "/api/flights", {"code": "MU2", "carousel": "CR1", "chute": "B"})
    assert status == 201
    status, bag = call("POST", "/api/bags/checkin", {"barcode": "9001", "flight_id": flight_a["id"]})
    assert status == 201
    call("POST", f"/api/bags/{bag['id']}/line/enter")
    call("POST", "/api/line/advance", {"cells": 9})
    status, result = call(
        "POST",
        f"/api/bags/{bag['id']}/rebook",
        {"flight_id": flight_b["id"], "request_id": "http-1"},
    )
    assert status == 202 and result["order"]["verdict"] == "let_ride"
    call("POST", "/api/line/advance", {"cells": 3})
    order_id = result["order"]["id"]
    status, reconciliation = call("GET", f"/api/rebooks/{order_id}/reconcile")
    assert status == 200 and reconciliation["matches"] is True
    status, integrity = call("GET", "/api/rebooks/integrity")
    assert status == 200 and integrity["holds"] is True
