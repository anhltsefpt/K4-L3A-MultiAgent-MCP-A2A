"""Classification priority, policy binding and verifier invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest

from student_agent.evidence import AuthoritativeView, build_view
from student_agent.policy import classify, decide
from student_agent.verifier import verify

POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required", "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        },
        "late_delivery_seller": {
            "case_status": "action_required", "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            # The policy ships a sample id that belongs to a different order.
            "responsible_parties": [{"party_type": "seller", "party_id": "seller-elsewhere"}],
        },
        "valid_split_payment": {
            "case_status": "no_action", "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_type": "customer", "party_id": None}],
        },
    },
}


def _order(status: str = "delivered", **overrides):
    order = {
        "order_id": "order-1",
        "order_status": status,
        "order_purchase_timestamp": "2018-06-25T09:00:00-03:00",
        "order_approved_at": "2018-06-25T10:00:00-03:00",
        "order_delivered_carrier_date": "2018-06-27T09:00:00-03:00",
        "order_delivered_customer_date": "2018-07-04T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-07-05T09:00:00-03:00",
    }
    order.update(overrides)
    return order


def _captures(*amounts: str):
    return {
        "events": [
            {
                "event_at": f"2018-06-25T{10 + index}:00:00-03:00",
                "event_type": "captured",
                "amount_brl": amount,
                "status": "confirmed",
            }
            for index, amount in enumerate(amounts)
        ]
    }


def _view(order, timeline, **overrides):
    payload = {
        "order": order,
        "items": [{
            "order_item_id": "item-1", "seller_id": "seller-1",
            "shipping_limit_date": "2018-06-28T09:00:00-03:00",
            "price": "79.00", "freight_value": "10.00",
        }],
        "payments": [],
        "payment_timeline": timeline,
        "refund_timeline": None,
        "shipment": None,
        "sellers": [],
    }
    payload.update(overrides)
    return build_view(**payload)


def test_equal_captures_reconciling_to_total_are_a_valid_split() -> None:
    view = _view(_order(), _captures("44.50", "44.50"))
    assert classify(view)[0] == "valid_split_payment"


def test_equal_captures_overshooting_the_total_are_a_duplicate_charge() -> None:
    view = _view(_order(), _captures("64.00", "64.00"))
    assert classify(view)[0] == "duplicate_charge"


def test_cancellation_outranks_every_payment_signal() -> None:
    view = _view(_order(status="canceled"), _captures("64.00", "64.00"))
    assert classify(view)[0] == "canceled_order_paid"


def test_open_reconciliation_event_wins_over_refund_state() -> None:
    timeline = _captures("35.00")
    timeline["events"].append({
        "event_at": "2018-06-25T12:00:00-03:00",
        "event_type": "reconciliation_mismatch", "amount_brl": "35.00", "status": "open",
    })
    refunds = {"events": [{
        "event_at": "2018-06-26T09:00:00-03:00", "event_type": "refund_requested",
        "amount_brl": "35.00", "status": "pending",
    }]}
    view = _view(_order(), timeline, refund_timeline=refunds)
    assert classify(view)[0] == "payment_mismatch"


def test_failed_refund_outranks_pending_refund() -> None:
    refunds = {"events": [
        {"event_at": "2018-06-26T09:00:00-03:00", "event_type": "refund_requested",
         "amount_brl": "89.00", "status": "pending"},
        {"event_at": "2018-06-27T09:00:00-03:00", "event_type": "refund_requested",
         "amount_brl": "89.00", "status": "failed"},
    ]}
    view = _view(_order(), _captures("89.00"), refund_timeline=refunds)
    assert classify(view)[0] == "refund_failed"


def test_late_delivery_falls_back_to_the_handoff_deadline() -> None:
    order = _order(
        order_delivered_customer_date="2018-07-10T09:00:00-03:00",
        order_delivered_carrier_date="2018-06-30T09:00:00-03:00",
    )
    view = _view(order, _captures("89.00"))
    assert classify(view) == ("late_delivery_seller", "CARRIER_HANDOFF_AFTER_SHIPPING_LIMIT")


def test_on_time_handoff_blames_logistics() -> None:
    order = _order(order_delivered_customer_date="2018-07-10T09:00:00-03:00")
    view = _view(order, _captures("89.00"))
    assert classify(view) == ("late_delivery_logistics", "HANDOFF_ON_TIME_TRANSIT_LATE")


def test_clean_order_is_an_unsupported_claim() -> None:
    view = _view(_order(), _captures("89.00"))
    assert classify(view)[0] == "unsupported_claim"


def test_absent_order_is_insufficient_evidence() -> None:
    assert classify(AuthoritativeView())[0] == "insufficient_evidence"


def test_seller_identity_is_rebound_to_this_order() -> None:
    order = _order(order_delivered_customer_date="2018-07-10T09:00:00-03:00")
    view = _view(order, _captures("89.00"))
    view.seller_ids = ["seller-1"]
    decision = decide(view, POLICY)
    assert decision.primary_issue == "late_delivery_logistics"

    view.late_events = [{"actor": "seller"}]
    decision = decide(view, POLICY)
    assert decision.primary_issue == "late_delivery_seller"
    assert decision.responsible_parties == [{"party_type": "seller", "party_id": "seller-1"}]


def test_missing_policy_rule_degrades_instead_of_guessing() -> None:
    view = _view(_order(status="canceled"), _captures("79.00"))
    decision = decide(view, {"rules": {}})
    assert decision.case_status == "needs_investigation"
    assert decision.refund_brl == Decimal("0")
    assert decision.confidence <= 0.5


@pytest.fixture
def output() -> dict:
    return {
        "assessment": {
            "primary_issue": "valid_split_payment",
            "case_status": "no_action",
            "confidence": 0.9,
        },
        "root_cause_analysis": {
            "responsible_parties": [{"party_type": "seller", "party_id": "seller-elsewhere"}]
        },
        "evidence_refs": ["ev_" + "a" * 24],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 25.0,
            "refund_lines": [],
        },
        "resolution_actions": ["document_no_action"],
        "claim_assessments": [],
    }


def test_verifier_strips_money_from_a_no_action_verdict(output: dict) -> None:
    view = AuthoritativeView(order_id="order-1", seller_ids=["seller-1"])
    notes = verify(output, view, output["evidence_refs"])
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output["financial_resolution"]["refund_lines"] == []
    assert "NO_ACTION_REFUND_DROPPED" in notes


def test_verifier_rebinds_a_foreign_seller_and_caps_confidence(output: dict) -> None:
    view = AuthoritativeView(order_id="order-1", seller_ids=["seller-1"])
    notes = verify(output, view, output["evidence_refs"])
    party = output["root_cause_analysis"]["responsible_parties"][0]
    assert party == {"party_type": "seller", "party_id": "seller-1"}
    assert "SELLER_IDENTITY_REBOUND" in notes
    assert output["assessment"]["confidence"] <= 0.70


def test_verifier_collapses_an_unattributable_seller_to_unknown(output: dict) -> None:
    view = AuthoritativeView(order_id="order-1", seller_ids=[])
    verify(output, view, output["evidence_refs"])
    assert output["root_cause_analysis"]["responsible_parties"][0]["party_type"] == "unknown"


def test_verifier_rebalances_refund_lines_against_the_headline_amount(output: dict) -> None:
    output["assessment"]["case_status"] = "action_required"
    output["assessment"]["primary_issue"] = "late_delivery_seller"
    view = AuthoritativeView(order_id="order-1", seller_ids=["seller-1"])
    notes = verify(output, view, output["evidence_refs"])
    lines = output["financial_resolution"]["refund_lines"]
    assert "REFUND_LINES_REBALANCED" in notes
    assert sum(line["amount_brl"] for line in lines) == 25.0


def test_verifier_drops_claim_refs_that_are_not_cited(output: dict) -> None:
    output["claim_assessments"] = [{
        "claim_id": "claim-1", "verdict": "supported", "confidence": 0.9,
        "evidence_refs": ["ev_" + "a" * 24, "ev_" + "b" * 24],
    }]
    view = AuthoritativeView(order_id="order-1", seller_ids=["seller-1"])
    verify(output, view, output["evidence_refs"])
    assert output["claim_assessments"][0]["evidence_refs"] == ["ev_" + "a" * 24]
