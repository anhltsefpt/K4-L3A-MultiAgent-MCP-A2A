from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.state import AGENT_TOOLS, CaseState, Decision

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
REF = "ev_" + "a" * 24
CASE = {
    "case_id": "CASE_001",
    "policy_version": "EC_POLICY_V1",
    "customer_request": {
        "claimed_order_id": "order-1",
        "claims": [{"claim_id": "c-a", "topic": "canceled_order_paid"}],
    },
}


def test_to_output_before_decision_is_rejected() -> None:
    with pytest.raises(ValueError, match="before the policy agent"):
        CaseState.from_case(CASE).to_output()


def test_to_output_matches_public_schema() -> None:
    state = CaseState.from_case(CASE)
    state.add_entity("order_ids", "order-1")
    state.add_entity("order_ids", "order-1")  # duplicates must not break uniqueItems
    state.add_entity("seller_ids", None)
    state.decision = Decision(
        primary_issue="canceled_order_paid",
        case_status="action_required",
        confidence=0.9,
        ranked_causes=[{"cause_code": "ORDER_CANCELED_AFTER_CAPTURE", "rank": 1}],
        responsible_parties=[{"party_type": "platform", "party_id": None}],
        refund_lines=[
            {"reason_code": "issue_refund", "amount_brl": 79.0, "entity_id": "order-1"}
        ],
        resolution_actions=["issue_refund"],
        evidence_refs=[REF],
    )
    output = state.to_output()
    CONTRACTS.validate_output(output, "state output")
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["affected_entities"]["order_ids"] == ["order-1"]
    assert "claim_assessments" not in output


def test_from_case_reads_real_input_shape() -> None:
    state = CaseState.from_case(CASE)
    assert (state.case_id, state.order_id) == ("CASE_001", "order-1")
    assert state.claims[0].topic == "canceled_order_paid"


def test_tool_ownership_is_exclusive() -> None:
    owners: dict[str, str] = {}
    for agent, tools in AGENT_TOOLS.items():
        for tool in tools:
            assert tool not in owners, f"{tool} owned by {owners[tool]} and {agent}"
            owners[tool] = agent
    assert AGENT_TOOLS["coordinator"] == frozenset()
    assert AGENT_TOOLS["verifier-agent"] == frozenset()


class _FakeSession:
    def __init__(self, result: SimpleNamespace) -> None:
        self._result = result

    async def call_tool(self, name: str, arguments: dict[str, str]) -> SimpleNamespace:
        return self._result


def test_gateway_reads_snake_case_result_fields() -> None:
    envelope = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": REF,
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": {},
    }
    ok = SimpleNamespace(is_error=False, structured_content=envelope, content=[])
    gateway = EvidenceGateway(_FakeSession(ok), CONTRACTS)  # type: ignore[arg-type]
    result = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="o"))
    assert result["evidence_ref"] == REF

    failed = SimpleNamespace(
        is_error=True, structured_content=None, content=[SimpleNamespace(text="boom")]
    )
    gateway = EvidenceGateway(_FakeSession(failed), CONTRACTS)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="o"))
