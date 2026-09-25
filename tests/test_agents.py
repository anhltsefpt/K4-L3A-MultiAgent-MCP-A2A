from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import gather_evidence

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
OID = "order-1"
DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
}


def canceled_order_data() -> dict[str, Any]:
    """Shape of a real canceled case: late-by-seller event on a canceled order, no refunds."""
    payments = [
        {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "79.00"},
        {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "18.00"},
    ]
    return {
        "get_order": {"order_id": OID, "order_status": "canceled"},
        "get_order_items": [
            {"order_item_id": "item-1", "seller_id": "seller-1", "price": "79.00",
             "freight_value": "10.00"},
            {"order_item_id": "item-1", "seller_id": "seller-1", "price": "79.00",
             "freight_value": "18.00"},
        ],
        "get_payment_timeline": {
            "payments": payments,
            "events": [
                {"event_type": "captured", "status": "confirmed", "amount_brl": "79.00"},
                {"event_type": "captured", "status": "confirmed", "amount_brl": "18.00"},
            ],
        },
        "get_shipment_summary": {
            "order_id": OID, "order_status": "canceled", "delivered_customer_at": None,
            "estimated_delivery_at": "2017-12-30T09:00:00-03:00",
            "events": [{"event_type": "delivered_late", "actor": "seller", "status": "confirmed"}],
        },
        "get_sellers": [{"seller_id": "seller-1"}],
    }


class FakeGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []
        self.counter = 0

    async def call(self, tool: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        assert case_id == "CASE_001" and arguments.get("order_id", OID) == OID
        self.calls.append(tool)
        if tool not in self.data:
            raise RuntimeError(f"MCP tool {tool} failed")  # e.g. refund timeline w/o refunds
        self.counter += 1
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool}_{self.counter}".ljust(30, "x"),
            "result_hash": "sha256:" + "0" * 64,
            "domain": DOMAINS[tool],
            "data": self.data[tool],
        }


CASE = {
    "case_id": "CASE_001",
    "policy_version": "EC_POLICY_V1",
    "customer_request": {
        "claimed_order_id": OID,
        "claims": [{"claim_id": "a", "topic": "canceled_order_paid"}],
    },
}


def gather(data: dict[str, Any], tmp_path: Path) -> tuple[Any, FakeGateway, list[dict[str, Any]]]:
    fake = FakeGateway(data)
    path = tmp_path / "trace.jsonl"
    state = asyncio.run(gather_evidence(CASE, fake, TraceWriter(path, CONTRACTS)))  # type: ignore[arg-type]
    trace = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return state, fake, trace


def test_specialists_extract_facts_and_entities_from_evidence(tmp_path: Path) -> None:
    state, fake, _ = gather(canceled_order_data(), tmp_path)
    order = state.findings["order-agent"].signals
    payment = state.findings["payment-agent"].signals
    shipment = state.findings["shipment-agent"].signals
    assert order["order_status"] == "canceled" and order["item_freights"] == [10.0, 18.0]
    assert payment["total_paid"] == 97.0 and payment["refund_events"] == []
    assert shipment["delivery_events"][0]["actor"] == "seller"
    assert shipment["implicated_seller_ids"] == ["seller-1"]
    assert state.entities() == {
        "order_ids": [OID],
        "item_ids": ["item-1"],  # duplicates collapsed (schema requires uniqueItems)
        "seller_ids": ["seller-1"],
        "payment_references": ["1"],
        "shipment_ids": [OID],
    }
    assert "get_order_payments" not in fake.calls and "get_product_context" not in fake.calls


def test_sellers_only_queried_when_seller_is_implicated(tmp_path: Path) -> None:
    data = canceled_order_data()
    data["get_shipment_summary"]["events"] = []
    _, fake, _ = gather(data, tmp_path)
    assert "get_sellers" not in fake.calls


def test_refund_timeline_is_optional_and_missing_required_tool_is_reported(
    tmp_path: Path,
) -> None:
    data = canceled_order_data()
    del data["get_order"]
    state, _, trace = gather(data, tmp_path)
    assert state.findings["order-agent"].missing == ["get_order"]
    assert state.findings["payment-agent"].missing == []  # refund timeline error is not "missing"
    handoff = next(e for e in trace if e["event_type"] == "handoff" and e["actor"] == "order-agent")
    assert handoff["decision_code"] == "findings_partial"
    assert handoff["attributes"] == {"missing": "get_order"}


def test_trace_links_every_consumed_evidence_and_handoffs_carry_refs(tmp_path: Path) -> None:
    state, _, trace = gather(canceled_order_data(), tmp_path)
    consumed = [e["evidence_refs"][0] for e in trace if e["event_type"] == "tool_result_consumed"]
    assert sorted(consumed) == sorted(state.evidence)
    handed = {ref for e in trace if e["event_type"] == "handoff" for ref in e["evidence_refs"]}
    assert handed == set(state.evidence)
    assert sum(e["event_type"] == "task_assigned" for e in trace) == 3
