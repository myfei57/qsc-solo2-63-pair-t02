"""Console endpoints for in-transit tracking and rebook adjudication."""

from __future__ import annotations


def _flights(server):
    plant = server.plant
    old = plant.register_flight("CA1234", "CR1", "A")
    new = plant.register_flight("MU5678", "CR2", "C")
    return old, new


def _bag(server, flight, code: str = "BAG0001"):
    return server.plant.check_in(code, flight.id)


def test_rebook_endpoint_retargets_an_induction_bag(server):
    old, new = _flights(server)
    bag = _bag(server, old)

    response = server.call(
        "POST", f"/api/bags/{bag.id}/rebook", {"flight_id": new.id}
    )

    assert response.status == 201
    payload = _json(response)
    assert payload["decision"]["action"] == "retarget"
    assert payload["effective_action"] == "retarget"
    assert payload["decision"]["new_flight_id"] == new.id


def test_line_movement_and_interception_endpoints(server):
    old, new = _flights(server)
    bag = _bag(server, old, "BAGLINE")

    entered = _json(server.call("POST", f"/api/line/enter/{bag.id}"))
    assert entered["location"] == "line"

    advanced = _json(server.call("POST", f"/api/line/advance/{bag.id}", {"seconds": 5}))
    assert advanced["mile"] == 5

    position = _json(server.call("GET", f"/api/line/bag/{bag.id}"))
    assert position["location"] == "line"
    assert position["interceptable"] is True
    assert position["divert_point"] == 20


def test_reroute_outcome_and_recovery_flow_via_api(server):
    plant = server.plant
    old, new = _flights(server)
    bag = _bag(server, old, "BAGRR")
    from bagsort.model.entities import Reading

    plant.enter_line(bag.id)
    plant.sorter.plan(
        plant.bag(bag.id),
        Reading(
            barcode=bag.barcode,
            bag_id=bag.id,
            sequence=bag.sequence,
            epoch=bag.epoch,
            committed=True,
        ),
    )
    plant.advance_bag(bag.id, 80)  # past CHC divert point

    decision = _json(
        server.call("POST", f"/api/bags/{bag.id}/rebook", {"flight_id": new.id})
    )
    assert decision["effective_action"] == "continue"

    recovery = _json(server.call("GET", "/api/rebook/recovery"))
    assert len(recovery) == 1
    assert recovery[0]["chute_id"] == "T1-CHA"
    assert recovery[0]["new_flight_id"] == new.id


def test_accepted_reroute_command_report_is_reflected_in_effective_action(server):
    plant = server.plant
    old, new = _flights(server)
    bag = _bag(server, old, "BAGOK")
    plant.enter_line(bag.id)
    plant.advance_bag(bag.id, 5)
    order, _decision = plant.rebook_bag(bag.id, new.id)

    outcome = _json(
        server.call(
            "POST",
            f"/api/rebook/orders/{order.id}/outcome",
            {"accepted": True, "detail": "diverter armed"},
        )
    )
    assert outcome["command"]["state"] == "accepted"
    assert outcome["effective_action"] == "reroute"


def test_reconciliation_endpoint_agrees_with_audit(server):
    plant = server.plant
    old, new = _flights(server)
    bag = _bag(server, old)
    plant.rebook_bag(bag.id, new.id)

    report = _json(server.call("GET", "/api/rebook/reconciliation"))
    assert report["ok"] is True
    assert report["audit_intact"] is True

    policy = _json(server.call("GET", "/api/rebook/policy"))
    assert policy["chosen"] == "adjust-pending-let-sorted-finish"
    assert len(policy["options"]) == 2
    rejected = [opt for opt in policy["options"] if not opt["chosen"]][0]
    assert "recall" in rejected["id"]

    decisions = _json(server.call("GET", "/api/rebook/decisions"))
    orders = _json(server.call("GET", "/api/rebook/orders"))
    assert len(decisions) == 1
    assert orders[0]["effective_action"] == "retarget"

    # audit timeline for the bag exists and chains up
    audit = _json(server.call("GET", f"/api/audit/bag/{bag.id}"))
    assert any(entry["kind"] == "rebook_decision" for entry in audit)


def test_orders_and_decision_listings_and_layout(server):
    plant = server.plant
    old, new = _flights(server)
    bag = _bag(server, old)
    plant.rebook_bag(bag.id, new.id)

    layout = _json(
        server.call(
            "POST", "/api/line/divert-point", {"chute_id": "T1-CHX", "mile": 33}
        )
    )
    assert layout == {"chute_id": "T1-CHX", "mile": 33}
    described = _json(server.call("GET", "/api/line/layout"))
    assert described["T1-CHX"] == 33

    bag_decisions = _json(server.call("GET", f"/api/bags/{bag.id}/rebook-decisions"))
    assert [d["action"] for d in bag_decisions] == ["retarget"]


def _json(response):
    import json

    return json.loads(response.body.decode("utf-8"))
