from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.evidence import AgentGateway
from student_agent.state import VERIFIER_AGENT
from student_agent.trace import TraceWriter
from student_agent.verifier import MAX_CONFIDENCE, verify
from student_agent.workflow import gather_evidence, solve_case
from test_agents import CASE, CONTRACTS, DOMAINS, OID, FakeGateway

DOMAINS["get_policy"] = "policy"

RULES = {
    "canceled_order_paid": ("action_required", "issue_refund", 79.0, [("platform", None)]),
    "unavailable_order_paid": (
        "action_required", "issue_refund", 89.0, [("seller", "seller-template")]
    ),
    "late_delivery_seller": (
        "action_required", "refund_freight", 18.0, [("seller", "seller-template")]
    ),
    "late_delivery_logistics": (
        "action_required", "refund_freight", 16.0, [("logistics_provider", None)]
    ),
    "duplicate_charge": (
        "action_required", "refund_duplicate_charge", 64.0, [("payment_provider", None)]
    ),
    "payment_mismatch": (
        "action_required", "reconcile_payment", 35.0, [("payment_provider", None)]
    ),
    "refund_failed": ("action_required", "retry_refund", 52.0, [("payment_provider", None)]),
    "refund_pending": ("needs_investigation", "monitor_refund", 0.0, [("payment_provider", None)]),
    "unsupported_claim": ("no_action", "document_no_action", 0.0, [("customer", None)]),
    "valid_split_payment": ("no_action", "document_no_action", 0.0, [("customer", None)]),
}
POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        issue: {
            "case_status": status,
            "recommended_action": action,
            "refund_brl": refund,
            "responsible_parties": [{"party_type": t, "party_id": i} for t, i in parties],
        }
        for issue, (status, action, refund, parties) in RULES.items()
    },
}


def clean_order() -> dict[str, Any]:
    """A delivered, on-time, correctly paid order: nothing wrong with it."""
    return {
        "get_order": {"order_id": OID, "order_status": "delivered"},
        "get_order_items": [
            {"order_item_id": "item-1", "seller_id": "seller-real", "price": "79.00",
             "freight_value": "10.00"},
        ],
        "get_payment_timeline": {
            "payments": [
                {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "89.00"}
            ],
            "events": [{"event_type": "captured", "status": "confirmed", "amount_brl": "89.00"}],
        },
        "get_shipment_summary": {
            "order_id": OID, "order_status": "delivered",
            "delivered_customer_at": "2018-05-02T09:00:00-03:00",
            "estimated_delivery_at": "2018-05-03T09:00:00-03:00", "events": [],
        },
        "get_policy": POLICY,
    }


def late_by(actor: str, data: dict[str, Any]) -> dict[str, Any]:
    ship = data["get_shipment_summary"]
    ship["delivered_customer_at"] = "2018-05-08T09:00:00-03:00"
    ship["events"] = [{"event_type": "delivered_late", "actor": actor, "status": "confirmed"}]
    if actor == "seller":
        data["get_sellers"] = [{"seller_id": "seller-real"}]
    return data


def run(data: dict[str, Any], topic: str, tmp_path: Path) -> tuple[dict[str, Any], list[dict]]:
    case = copy.deepcopy(CASE)
    case["customer_request"]["claims"] = [
        {"claim_id": "c-a", "topic": topic},
        {"claim_id": "c-b", "topic": "requested_full_refund"},
    ]
    path = tmp_path / "trace.jsonl"
    trace = TraceWriter(path, CONTRACTS)
    output = asyncio.run(solve_case(case, FakeGateway(data), trace))  # type: ignore[arg-type]
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return output, events


def test_claim_contradicted_by_evidence_is_not_echoed(tmp_path: Path) -> None:
    output, _ = run(clean_order(), "canceled_order_paid", tmp_path)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    verdicts = {c["claim_id"]: c["verdict"] for c in output["claim_assessments"]}
    assert verdicts == {"c-a": "unsupported", "c-b": "unsupported"}


def test_evidence_beats_claim_when_a_different_issue_is_supported(tmp_path: Path) -> None:
    output, _ = run(late_by("logistics_provider", clean_order()), "late_delivery_seller", tmp_path)
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    parties = output["root_cause_analysis"]["responsible_parties"]
    assert [p["party_type"] for p in parties] == ["logistics_provider"]
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["assessment"]["confidence"] <= 0.75  # claim was contradicted: not confident


def test_seller_responsibility_uses_the_orders_seller_not_the_policy_template(
    tmp_path: Path,
) -> None:
    output, _ = run(late_by("seller", clean_order()), "late_delivery_seller", tmp_path)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": "seller-real"}
    ]
    assert output["affected_entities"]["seller_ids"] == ["seller-real"]
    assert output["financial_resolution"]["refund_lines"] == [
        {"reason_code": "refund_freight", "amount_brl": 18.0, "entity_id": OID}
    ]


def test_event_contradicting_timestamps_is_recorded_as_conflict_and_unsupported(
    tmp_path: Path,
) -> None:
    data = clean_order()
    data["get_shipment_summary"]["events"] = [
        {"event_type": "delivered_late", "actor": "logistics_provider", "status": "confirmed"}
    ]
    output, _ = run(data, "late_delivery_logistics", tmp_path)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    [conflict] = output["data_conflicts"]
    assert conflict["field"] == "delivery_lateness"
    assert output["assessment"]["confidence"] < 0.8


def test_missing_core_evidence_yields_insufficient_evidence_not_a_guess(tmp_path: Path) -> None:
    data = clean_order()
    del data["get_shipment_summary"]
    output, events = run(data, "late_delivery_seller", tmp_path)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"] == {
        "currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []
    }
    assert output["assessment"]["confidence"] <= 0.3
    assert any(e["event_type"] == "verification_completed" for e in events)


def test_lifecycle_events_are_complete_and_ordered(tmp_path: Path) -> None:
    _, events = run(clean_order(), "unsupported_claim", tmp_path)
    types = [e["event_type"] for e in events]
    first = {t: types.index(t) for t in set(types)}
    assert first["task_assigned"] < first["tool_result_consumed"] < first["handoff"]
    assert first["handoff"] < first["policy_decided"] < first["verification_completed"]
    assert {"order-agent", "payment-agent", "shipment-agent", "policy-agent", "verifier-agent"} <= {
        e["actor"] for e in events
    }


def test_every_cited_evidence_ref_was_consumed_in_the_trace(tmp_path: Path) -> None:
    output, events = run(late_by("seller", clean_order()), "late_delivery_seller", tmp_path)
    consumed = {r for e in events if e["event_type"] == "tool_result_consumed"
                for r in e["evidence_refs"]}
    assert set(output["evidence_refs"]) <= consumed
    for claim in output["claim_assessments"]:
        assert set(claim["evidence_refs"]) <= set(output["evidence_refs"])


def test_confidence_is_never_certain(tmp_path: Path) -> None:
    for topic in RULES:
        output, _ = run(clean_order(), topic, tmp_path)
        assert 0 < output["assessment"]["confidence"] <= MAX_CONFIDENCE < 1.0
        (tmp_path / "trace.jsonl").unlink()


def test_verifier_downgrades_an_inconsistent_decision(tmp_path: Path) -> None:
    data = late_by("logistics_provider", clean_order())
    fake = FakeGateway(data)
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)

    async def scenario() -> dict[str, Any]:
        from student_agent.policy import policy_agent

        state = await gather_evidence(CASE, fake, trace)  # type: ignore[arg-type]
        gateway = AgentGateway("policy-agent", state, fake, trace, asyncio.Lock())  # type: ignore[arg-type]
        await policy_agent(state, gateway)
        assert state.decision is not None
        state.decision.refund_lines[0]["amount_brl"] = 999.0  # tampered: exceeds amount paid
        verifier = AgentGateway(VERIFIER_AGENT, state, fake, trace, asyncio.Lock())  # type: ignore[arg-type]
        return verify(state, verifier, CONTRACTS)

    output = asyncio.run(scenario())
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    done = next(e for e in events if e["event_type"] == "verification_completed")
    assert done["decision_code"] == "downgraded"


@pytest.mark.parametrize("topic", sorted(RULES))
def test_all_outputs_pass_the_public_schema(topic: str, tmp_path: Path) -> None:
    output, _ = run(clean_order(), topic, tmp_path)
    CONTRACTS.validate_output(output, topic)
