"""Policy agent: turn specialist findings into a decision using the MCP `get_policy` rules.

The decision is deterministic. For every issue in the public schema we test whether the
consumed evidence supports it; the customer's claimed topic is used only to choose between
issues that the evidence *already* supports, never to invent one.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .agents import money
from .evidence import AgentGateway
from .state import CaseState, Claim, Decision

NON_ISSUE_TOPICS = frozenset({"requested_full_refund"})

# Order used when the claimed topic is not supported but the evidence supports something else.
FALLBACK_PRIORITY = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "duplicate_charge",
    "payment_mismatch",
    "refund_failed",
    "refund_pending",
    "valid_split_payment",
)

# Evidence domains that justify each conclusion (used to pick which refs to cite).
ISSUE_DOMAINS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order", "payment", "policy"),
    "unavailable_order_paid": ("order", "payment", "policy"),
    "late_delivery_seller": ("order", "item", "shipment", "seller", "payment", "policy"),
    "late_delivery_logistics": ("order", "shipment", "payment", "policy"),
    "duplicate_charge": ("order", "payment", "policy"),
    "valid_split_payment": ("order", "item", "payment", "policy"),
    "payment_mismatch": ("order", "payment", "policy"),
    "refund_pending": ("payment", "refund", "policy"),
    "refund_failed": ("payment", "refund", "policy"),
    "unsupported_claim": ("order", "shipment", "payment", "policy"),
    "insufficient_evidence": ("order", "item", "payment", "shipment", "refund", "policy"),
}

CORE_TOOLS = ("get_order", "get_payment_timeline", "get_shipment_summary", "get_policy")


def _signals(state: CaseState) -> dict[str, dict[str, Any]]:
    return {agent: finding.signals for agent, finding in state.findings.items()}


def _late_actors(shipment: dict[str, Any]) -> set[str]:
    return {
        e["actor"]
        for e in shipment.get("delivery_events", [])
        if e["type"] == "delivered_late" and e["status"] == "confirmed" and e["actor"]
    }


def _valid_split(payments: list[dict[str, Any]], line_totals: set[float]) -> bool:
    """A voucher + card pair of equal value that adds up to one order line is a legit split."""
    vouchers = [p["value"] for p in payments if p["type"] == "voucher"]
    cards = [p["value"] for p in payments if p["type"] != "voucher"]
    return any(round(v + c, 2) in line_totals for v in vouchers for c in cards if v == c)


def supported_issues(state: CaseState) -> list[str]:
    """Issues whose evidence predicate holds, in FALLBACK_PRIORITY order."""
    order = state.findings["order-agent"].signals
    payment = state.findings["payment-agent"].signals
    shipment = state.findings["shipment-agent"].signals
    status = order.get("order_status") or shipment.get("order_status")
    payments = payment.get("payments", [])
    events = payment.get("payment_events", [])
    refunds = payment.get("refund_events", [])
    paid = any(e["type"] == "captured" and e["status"] == "confirmed" for e in events)
    late_days = shipment.get("days_late")
    delivered_late = status == "delivered" and late_days is not None and late_days > 0
    actors = _late_actors(shipment)
    line_totals = {
        round(p + f, 2)
        for p, f in zip(order.get("item_prices", []), order.get("item_freights", []), strict=False)
    }
    counts = Counter((p["type"], p["value"]) for p in payments)

    checks = {
        "canceled_order_paid": status == "canceled" and paid,
        "unavailable_order_paid": status == "unavailable" and paid,
        "late_delivery_seller": delivered_late and "seller" in actors,
        "late_delivery_logistics": delivered_late and "logistics_provider" in actors,
        "duplicate_charge": any(count >= 2 for count in counts.values())
        and not _valid_split(payments, line_totals),
        "payment_mismatch": any(
            e["type"] == "reconciliation_mismatch" and e["status"] == "open" for e in events
        ),
        "refund_failed": any(r["status"] == "failed" for r in refunds),
        "refund_pending": any(r["status"] == "pending" for r in refunds),
        "valid_split_payment": _valid_split(payments, line_totals),
    }
    return [issue for issue in FALLBACK_PRIORITY if checks[issue]]


def find_conflicts(state: CaseState) -> list[dict[str, Any]]:
    order = state.findings["order-agent"].signals
    shipment = state.findings["shipment-agent"].signals
    conflicts: list[dict[str, Any]] = []
    late_events = [e for e in shipment.get("delivery_events", []) if e["type"] == "delivered_late"]
    if late_events and order.get("order_status") not in (None, "delivered"):
        conflicts.append(
            {
                "field": "delivery_status",
                "sources": ["order", "shipment_events"],
                "selected_source": "order",
                "resolution_code": "ORDER_STATUS_AUTHORITATIVE",
            }
        )
    days_late = shipment.get("days_late")
    if late_events and days_late is not None and days_late <= 0:
        conflicts.append(
            {
                "field": "delivery_lateness",
                "sources": ["shipment_events", "shipment_timestamps"],
                "selected_source": "shipment_timestamps",
                "resolution_code": "TIMESTAMPS_OVERRIDE_EVENT",
            }
        )
    if (
        order.get("order_status")
        and shipment.get("order_status")
        and order["order_status"] != shipment["order_status"]
    ):
        conflicts.append(
            {
                "field": "order_status",
                "sources": ["order", "shipment"],
                "selected_source": "order",
                "resolution_code": "ORDER_STATUS_AUTHORITATIVE",
            }
        )
    return conflicts


def choose_issue(state: CaseState, supported: list[str]) -> tuple[str, str]:
    """Return (primary_issue, basis). The claim only breaks ties among supported issues."""
    claimed = [c.topic for c in state.claims if c.topic not in NON_ISSUE_TOPICS]
    for topic in claimed:
        if topic in supported:
            return topic, "claim_confirmed" if len(supported) == 1 else "claim_tiebreak"
    if supported:
        return supported[0], "evidence_only"
    return "unsupported_claim", "no_support"


def _seller_party(state: CaseState) -> str | None:
    shipment = state.findings["shipment-agent"].signals
    order = state.findings["order-agent"].signals
    sellers = shipment.get("implicated_seller_ids") or order.get("seller_ids") or []
    return sellers[0] if sellers else None


def _parties(state: CaseState, rule: dict[str, Any]) -> list[dict[str, Any]]:
    parties = []
    for party in rule["responsible_parties"]:
        party_id = party["party_id"]
        if party["party_type"] == "seller":
            # The public policy carries a template id; the accountable seller is the one
            # attached to this order's evidence.
            party_id = _seller_party(state) or party_id
        parties.append({"party_type": party["party_type"], "party_id": party_id})
    return parties


def _cited_refs(state: CaseState, issue: str, conflicts: list[dict[str, Any]]) -> list[str]:
    domains = set(ISSUE_DOMAINS[issue])
    if any("shipment" in source for c in conflicts for source in c["sources"]):
        domains.add("shipment")
    return [r.evidence_ref for r in state.evidence.values() if r.domain in domains]


def _claim_assessments(
    state: CaseState, issue: str, refund: float, refs: list[str]
) -> list[dict[str, Any]]:
    """Verdict per customer claim; every cited ref is already part of the output evidence."""
    total_paid = state.findings["payment-agent"].signals.get("total_paid") or 0.0
    by_domain: dict[str, list[str]] = {}
    for record in state.evidence.values():
        by_domain.setdefault(record.domain, []).append(record.evidence_ref)
    assessments = []
    for claim in state.claims:
        cited = list(refs)
        if claim.topic == "requested_full_refund":
            cited = by_domain.get("payment", []) + by_domain.get("policy", [])
            if refund <= 0:
                verdict = "insufficient_evidence" if issue == "refund_pending" else "unsupported"
            else:
                verdict = "supported" if refund >= total_paid else "partially_supported"
        elif claim.topic == issue and issue != "unsupported_claim":
            verdict = "supported"
        else:
            verdict = "unsupported"
            cited = sum((by_domain.get(d, []) for d in ("order", "payment", "shipment")), [])
        assessments.append(_assessment(claim, verdict, cited))
    return assessments


def _assessment(claim: Claim, verdict: str, refs: list[str]) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "verdict": verdict,
        "confidence": 0.0,  # filled in by the verifier's calibration
        "evidence_refs": list(dict.fromkeys(refs)),
    }


def decide(state: CaseState, policy_data: dict[str, Any] | None) -> Decision:
    """Pure decision function: findings + policy rules -> Decision."""
    missing = [tool for tool in CORE_TOOLS if state.by_tool(tool) is None]
    if missing:
        return insufficient_decision(state, missing)
    rules = (policy_data or {}).get("rules", {})
    conflicts = find_conflicts(state)
    supported = supported_issues(state)
    issue, basis = choose_issue(state, supported)
    rule = rules.get(issue)
    if rule is None:
        return insufficient_decision(state, [f"policy_rule:{issue}"])

    refund = money(rule["refund_brl"])
    order_id = state.order_id
    lines = (
        [{"reason_code": rule["recommended_action"], "amount_brl": refund, "entity_id": order_id}]
        if refund > 0
        else []
    )
    refs = _cited_refs(state, issue, conflicts)
    assessments = _claim_assessments(state, issue, refund, refs)
    refs = list(dict.fromkeys(refs + [r for a in assessments for r in a["evidence_refs"]]))
    return Decision(
        primary_issue=issue,
        case_status=rule["case_status"],
        confidence=0.0,  # calibrated by the verifier
        ranked_causes=[{"cause_code": issue.upper(), "rank": 1}],
        responsible_parties=_parties(state, rule),
        refund_lines=lines,
        resolution_actions=[rule["recommended_action"]],
        claim_assessments=assessments,
        data_conflicts=conflicts,
        evidence_refs=refs,
        basis=basis,
        supported=supported,
    )


def insufficient_decision(state: CaseState, missing: list[str]) -> Decision:
    refs = [r.evidence_ref for r in state.evidence.values()]
    return Decision(
        primary_issue="insufficient_evidence",
        case_status="needs_investigation",
        ranked_causes=[{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
        responsible_parties=[{"party_type": "unknown", "party_id": None}],
        resolution_actions=["collect_missing_evidence"],
        claim_assessments=[
            _assessment(claim, "insufficient_evidence", refs) for claim in state.claims
        ],
        evidence_refs=refs,
        basis="insufficient_evidence",
        missing=missing,
    )


async def policy_agent(state: CaseState, gateway: AgentGateway) -> Decision:
    policy = await gateway.call("get_policy", policy_version=state.policy_version)
    decision = decide(state, policy.data if policy else None)
    state.decision = decision
    gateway.emit(
        "policy_decided",
        target="verifier-agent",
        decision_code=decision.primary_issue,
        evidence_refs=decision.evidence_refs[:20],
        attributes={"basis": decision.basis, "case_status": decision.case_status},
    )
    return decision
