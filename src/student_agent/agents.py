"""Specialist agents: collect authoritative evidence over MCP and extract plain facts.

Specialists never decide the case. They return a `Finding` whose `signals` are facts read
from evidence (with the evidence refs that support them); the policy agent draws conclusions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .evidence import AgentGateway
from .state import ORDER_AGENT, PAYMENT_AGENT, SHIPMENT_AGENT, CaseState, EvidenceRecord, Finding


def money(value: Any) -> float:
    """MCP amounts are decimal strings ('79.00'); normalise to a 2-decimal float."""
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _rows(record: EvidenceRecord | None) -> list[dict[str, Any]]:
    return list(record.data) if record and isinstance(record.data, list) else []


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


async def order_agent(state: CaseState, gateway: AgentGateway) -> Finding:
    finding = Finding(ORDER_AGENT)
    order = await gateway.call("get_order", order_id=state.order_id)
    items = await gateway.call("get_order_items", order_id=state.order_id)

    if order is None:
        finding.missing.append("get_order")
    else:
        finding.evidence_refs.append(order.evidence_ref)
        finding.signals["order_status"] = order.data.get("order_status")
        finding.signals["purchased_at"] = order.data.get("order_purchase_timestamp")
        state.add_entity("order_ids", order.data.get("order_id"))
    if items is None:
        finding.missing.append("get_order_items")
    else:
        rows = _rows(items)
        finding.evidence_refs.append(items.evidence_ref)
        finding.signals["item_prices"] = [money(r["price"]) for r in rows]
        finding.signals["item_freights"] = [money(r["freight_value"]) for r in rows]
        for row in rows:
            state.add_entity("item_ids", row.get("order_item_id"))
            state.add_entity("seller_ids", row.get("seller_id"))
        finding.signals["seller_ids"] = _unique([r.get("seller_id") for r in rows])
    return finding


async def payment_agent(state: CaseState, gateway: AgentGateway) -> Finding:
    finding = Finding(PAYMENT_AGENT)
    # The timeline is a superset of get_order_payments (base payments + lifecycle events).
    timeline = await gateway.call("get_payment_timeline", order_id=state.order_id)
    # Orders that were never refunded make this tool error out: that means "no refund events".
    refunds = await gateway.call("get_refund_timeline", optional=True, order_id=state.order_id)

    if timeline is None:
        finding.missing.append("get_payment_timeline")
    else:
        finding.evidence_refs.append(timeline.evidence_ref)
        payments = timeline.data.get("payments", [])
        events = timeline.data.get("events", [])
        finding.signals["payments"] = [
            {
                "sequential": p["payment_sequential"],
                "type": p["payment_type"],
                "value": money(p["payment_value"]),
            }
            for p in payments
        ]
        finding.signals["payment_events"] = [
            {
                "type": e["event_type"],
                "status": e.get("status"),
                "amount": money(e["amount_brl"]),
            }
            for e in events
        ]
        paid = sum(Decimal(str(p["payment_value"])) for p in payments)
        finding.signals["total_paid"] = money(paid)
        for payment in payments:
            state.add_entity("payment_references", str(payment["payment_sequential"]))
    if refunds is None:
        finding.signals["refund_events"] = []
    else:
        finding.evidence_refs.append(refunds.evidence_ref)
        finding.signals["refund_events"] = [
            {
                "type": e["event_type"],
                "status": e.get("status"),
                "amount": money(e["amount_brl"]),
            }
            for e in refunds.data.get("events", [])
        ]
    return finding


async def shipment_agent(state: CaseState, gateway: AgentGateway) -> Finding:
    finding = Finding(SHIPMENT_AGENT)
    shipment = await gateway.call("get_shipment_summary", order_id=state.order_id)
    if shipment is None:
        finding.missing.append("get_shipment_summary")
        return finding

    data = shipment.data
    finding.evidence_refs.append(shipment.evidence_ref)
    delivered = moment(data.get("delivered_customer_at"))
    estimated = moment(data.get("estimated_delivery_at"))
    finding.signals["order_status"] = data.get("order_status")
    finding.signals["delivered_at"] = data.get("delivered_customer_at")
    finding.signals["estimated_at"] = data.get("estimated_delivery_at")
    finding.signals["days_late"] = None
    if delivered and estimated:
        finding.signals["days_late"] = round((delivered - estimated).total_seconds() / 86400, 2)
    events = data.get("events", [])
    finding.signals["delivery_events"] = [
        {"type": e["event_type"], "actor": e.get("actor"), "status": e.get("status")}
        for e in events
    ]
    state.add_entity("shipment_ids", data.get("order_id"))

    # Seller records are only relevant when a seller is implicated in the delivery.
    if any(e.get("actor") == "seller" for e in events):
        sellers = await gateway.call("get_sellers", order_id=state.order_id)
        if sellers is None:
            finding.missing.append("get_sellers")
        else:
            finding.evidence_refs.append(sellers.evidence_ref)
            finding.signals["implicated_seller_ids"] = _unique(
                [row.get("seller_id") for row in _rows(sellers)]
            )
    return finding
