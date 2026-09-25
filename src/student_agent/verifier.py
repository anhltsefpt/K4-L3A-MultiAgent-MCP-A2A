"""Verifier agent: calibrate confidence and enforce cross-field invariants before finalizing.

The verifier has no MCP tools. It only reads what the other agents recorded in `CaseState`
and either accepts the decision, or downgrades it to `insufficient_evidence` (never a guess).
"""

from __future__ import annotations

from typing import Any

from .contracts import ContractError, Contracts
from .evidence import AgentGateway
from .policy import insufficient_decision
from .state import CaseState, Decision

MAX_CONFIDENCE = 0.95

# Starting confidence by how the primary issue was reached.
BASE_CONFIDENCE = {
    "claim_confirmed": 0.92,  # the only issue the evidence supports is the claimed one
    "claim_tiebreak": 0.80,  # several issues are supported; the claim picked among them
    "evidence_only": 0.70,  # the claim was contradicted; evidence points elsewhere
    "no_support": 0.80,  # nothing supports the claim, evidence is complete
    "insufficient_evidence": 0.25,
}
CONFLICT_PENALTY = 0.05
MAX_WITH_CONFLICT = 0.90
REFUND_CLAIM_CAP = 0.85


def calibrate(state: CaseState, decision: Decision) -> None:
    """Set the overall and per-claim confidence from evidence quality (never 1.0)."""
    confidence = BASE_CONFIDENCE.get(decision.basis, 0.5)
    confidence -= CONFLICT_PENALTY * len(decision.data_conflicts)
    if decision.data_conflicts:
        confidence = min(confidence, MAX_WITH_CONFLICT)
    decision.confidence = round(max(0.05, min(confidence, MAX_CONFIDENCE)), 2)
    topics = {claim.claim_id: claim.topic for claim in state.claims}
    for assessment in decision.claim_assessments:
        # The refund claim also depends on policy amounts, so it is never more certain.
        if topics.get(assessment["claim_id"]) == "requested_full_refund":
            assessment["confidence"] = min(decision.confidence, REFUND_CLAIM_CAP)
        else:
            assessment["confidence"] = decision.confidence


def violations(state: CaseState, output: dict[str, Any], contracts: Contracts) -> list[str]:
    found: list[str] = []
    try:
        contracts.validate_output(output, f"outputs/{state.case_id}.json")
    except ContractError as exc:
        return [f"schema: {exc}"]

    decision = state.decision
    assert decision is not None
    known = set(state.evidence)
    cited = set(output["evidence_refs"])
    if output["case_id"] != state.case_id:
        found.append("case_id mismatch")
    if not set(output["affected_entities"]["order_ids"]) <= {state.order_id}:
        found.append("entity scope: order id outside this case")
    if not cited <= known:
        found.append("evidence ownership: unknown evidence_ref")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= cited:
            found.append(f"claim linkage: {claim['claim_id']} cites evidence not in evidence_refs")

    resolution = output["financial_resolution"]
    lines_total = round(sum(line["amount_brl"] for line in resolution["refund_lines"]), 2)
    if lines_total != resolution["recommended_refund_brl"]:
        found.append("money: refund lines do not sum to recommended_refund_brl")
    total_paid = state.findings["payment-agent"].signals.get("total_paid")
    if total_paid is not None and resolution["recommended_refund_brl"] > total_paid:
        found.append("money: refund exceeds the amount paid")

    status = output["assessment"]["case_status"]
    if status == "no_action" and (resolution["recommended_refund_brl"] > 0):
        found.append("consistency: no_action with a refund")
    actions = output["resolution_actions"]
    if not actions or len(set(actions)) != len(actions):
        found.append("consistency: actions must be non-empty and unique")

    found += _policy_alignment(state, output)
    confidence = output["assessment"]["confidence"]
    if not 0 <= confidence <= MAX_CONFIDENCE:
        found.append("confidence out of bounds")
    if output["data_conflicts"] and confidence > MAX_WITH_CONFLICT:
        found.append("confidence too high given data conflicts")
    return found


def _policy_alignment(state: CaseState, output: dict[str, Any]) -> list[str]:
    issue = output["assessment"]["primary_issue"]
    if issue == "insufficient_evidence":
        return []
    policy = state.by_tool("get_policy")
    rule = (policy.data.get("rules", {}) if policy else {}).get(issue)
    if rule is None:
        return [f"policy: no rule for {issue}"]
    found: list[str] = []
    parties = output["root_cause_analysis"]["responsible_parties"]
    if {p["party_type"] for p in parties} != {p["party_type"] for p in rule["responsible_parties"]}:
        found.append("responsibility: parties differ from the policy rule")
    seller_ids = set(output["affected_entities"]["seller_ids"])
    for party in parties:
        if party["party_type"] == "seller" and party["party_id"] not in seller_ids:
            found.append("responsibility: seller is not one of the order's sellers")
    if output["assessment"]["case_status"] != rule["case_status"]:
        found.append("consistency: case_status differs from the policy rule")
    if output["resolution_actions"] != [rule["recommended_action"]]:
        found.append("consistency: actions differ from the policy rule")
    if output["financial_resolution"]["recommended_refund_brl"] != rule["refund_brl"]:
        found.append("money: refund differs from the policy rule")
    return found


def verify(state: CaseState, agent: AgentGateway, contracts: Contracts) -> dict[str, Any]:
    """Verify (and if necessary downgrade) the decision; return the final public output."""
    decision = state.decision
    if decision is None:
        raise ValueError("verifier received no decision")
    calibrate(state, decision)
    output = state.to_output()
    problems = violations(state, output, contracts)
    outcome = "verified"
    if problems:
        state.decision = insufficient_decision(state, decision.missing or ["verification"])
        calibrate(state, state.decision)
        output = state.to_output()
        remaining = violations(state, output, contracts)
        if remaining:
            raise ValueError(f"{state.case_id}: unresolvable violations: {remaining}")
        outcome = "downgraded"
    state.verified = True
    agent.emit(
        "verification_completed",
        target="coordinator",
        decision_code=outcome,
        evidence_refs=output["evidence_refs"][:20],
        attributes={
            "violations": len(problems),
            "confidence": output["assessment"]["confidence"],
            "first_violation": problems[0][:80] if problems else None,
        },
    )
    return output
