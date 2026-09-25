"""State contract shared by the L3A agents.

Everything here is internal working state. Only `CaseState.to_output()` produces a public
object, and it emits exactly the fields of `l3a-output-v2.schema.json` (no extras).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Literal

from . import OUTPUT_SCHEMA_VERSION

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER_AGENT = "verifier-agent"

SPECIALISTS = (ORDER_AGENT, PAYMENT_AGENT, SHIPMENT_AGENT)

# Least-privilege tool matrix. Tools missing here (get_product_context, get_customer_history)
# are deliberately unassigned: they are off-topic for L3A and only risk evidence-relevance loss.
AGENT_TOOLS: dict[str, frozenset[str]] = {
    COORDINATOR: frozenset(),
    ORDER_AGENT: frozenset({"get_order", "get_order_items"}),
    PAYMENT_AGENT: frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    SHIPMENT_AGENT: frozenset({"get_shipment_summary", "get_sellers"}),
    POLICY_AGENT: frozenset({"get_policy"}),
    VERIFIER_AGENT: frozenset(),
}

PARTY_TYPES = frozenset(
    {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}
)
CASE_STATUSES = frozenset({"action_required", "no_action", "needs_investigation"})
MessageKind = Literal["task", "result", "handoff"]
EntityKind = Literal["order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids"]
ENTITY_KINDS: tuple[EntityKind, ...] = (
    "order_ids",
    "item_ids",
    "seller_ids",
    "payment_references",
    "shipment_ids",
)


def require_tools(discovered: list[str]) -> None:
    """Fail fast if tool discovery does not offer every tool the agents are assigned."""
    missing = sorted(set().union(*AGENT_TOOLS.values()) - set(discovered))
    if missing:
        raise RuntimeError(f"MCP Gateway is missing expected tools: {', '.join(missing)}")


class ToolNotPermitted(PermissionError):
    """An agent tried to call a tool outside its ownership."""


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    topic: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One MCP envelope, stored verbatim. `evidence_ref` is never generated or edited."""

    tool: str
    domain: str
    evidence_ref: str
    result_hash: str
    data: Any
    warnings: tuple[str, ...] = ()
    consumed_by: str = ""


@dataclass(frozen=True, slots=True)
class A2AMessage:
    """Envelope for agent-to-agent handoff, correlated by `case_id` + `message_id`."""

    case_id: str
    sender: str
    recipient: str
    kind: MessageKind
    body: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    message_id: str = field(default_factory=lambda: f"msg_{secrets.token_urlsafe(9)}")


@dataclass(slots=True)
class Finding:
    """What a specialist concluded from its own evidence; consumed by the policy agent."""

    agent: str
    signals: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Decision:
    """Policy agent result, shaped like the public output so the verifier can check it."""

    primary_issue: str = "insufficient_evidence"
    case_status: str = "needs_investigation"
    confidence: float = 0.0
    ranked_causes: list[dict[str, Any]] = field(default_factory=list)
    responsible_parties: list[dict[str, Any]] = field(default_factory=list)
    refund_lines: list[dict[str, Any]] = field(default_factory=list)
    resolution_actions: list[str] = field(default_factory=list)
    claim_assessments: list[dict[str, Any]] = field(default_factory=list)
    data_conflicts: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    # Internal (never serialized): how the issue was chosen, used by the verifier to calibrate.
    basis: str = "insufficient_evidence"
    supported: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def recommended_refund_brl(self) -> float:
        return round(sum(line["amount_brl"] for line in self.refund_lines), 2)


@dataclass(slots=True)
class CaseState:
    """Per-case blackboard. One instance per case, so evidence cannot cross cases."""

    case_id: str
    order_id: str
    policy_version: str
    claims: tuple[Claim, ...]
    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)  # keyed by evidence_ref
    findings: dict[str, Finding] = field(default_factory=dict)  # keyed by agent
    inbox: list[A2AMessage] = field(default_factory=list)
    entity_ids: dict[str, list[str]] = field(
        default_factory=lambda: {kind: [] for kind in ENTITY_KINDS}
    )
    decision: Decision | None = None
    verified: bool = False

    @classmethod
    def from_case(cls, case: dict[str, Any]) -> CaseState:
        request = case["customer_request"]
        return cls(
            case_id=case["case_id"],
            order_id=request["claimed_order_id"],
            policy_version=case["policy_version"],
            claims=tuple(Claim(c["claim_id"], c["topic"]) for c in request["claims"]),
        )

    def record(self, tool: str, envelope: dict[str, Any], agent: str) -> EvidenceRecord:
        record = EvidenceRecord(
            tool=tool,
            domain=envelope["domain"],
            evidence_ref=envelope["evidence_ref"],
            result_hash=envelope["result_hash"],
            data=envelope["data"],
            warnings=tuple(envelope.get("warnings", ())),
            consumed_by=agent,
        )
        self.evidence[record.evidence_ref] = record
        return record

    def by_tool(self, tool: str) -> EvidenceRecord | None:
        return next((r for r in self.evidence.values() if r.tool == tool), None)

    def send(self, sender: str, recipient: str, kind: MessageKind, **body: Any) -> A2AMessage:
        message = A2AMessage(self.case_id, sender, recipient, kind, body)
        self.inbox.append(message)
        return message

    def to_output(self) -> dict[str, Any]:
        """Serialize to the public L3A output. Field set is fixed by the JSON Schema."""
        if self.decision is None:
            raise ValueError("cannot build output before the policy agent has decided")
        d = self.decision
        entities = self.entities()
        output: dict[str, Any] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": self.case_id,
            "assessment": {
                "primary_issue": d.primary_issue,
                "case_status": d.case_status,
                "confidence": d.confidence,
            },
            "affected_entities": entities,
            "root_cause_analysis": {
                "ranked_causes": d.ranked_causes,
                "responsible_parties": d.responsible_parties,
            },
            "evidence_refs": d.evidence_refs,
            "data_conflicts": d.data_conflicts,
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": d.recommended_refund_brl,
                "refund_lines": d.refund_lines,
            },
            "resolution_actions": d.resolution_actions,
        }
        if d.claim_assessments:
            output["claim_assessments"] = d.claim_assessments
        return output

    def add_entity(self, kind: EntityKind, value: str | None) -> None:
        """Register an id read from consumed evidence (never from the customer message)."""
        if value and value not in self.entity_ids[kind]:
            self.entity_ids[kind].append(value)

    def entities(self) -> dict[str, list[str]]:
        return {kind: list(values) for kind, values in self.entity_ids.items()}
