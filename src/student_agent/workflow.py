from __future__ import annotations

import asyncio
from typing import Any

from .agents import order_agent, payment_agent, shipment_agent
from .evidence import AgentGateway
from .mcp_gateway import EvidenceGateway
from .policy import policy_agent
from .state import (
    AGENT_TOOLS,
    COORDINATOR,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    VERIFIER_AGENT,
    CaseState,
)
from .trace import TraceWriter
from .verifier import verify

SPECIALIST_RUNNERS = {
    ORDER_AGENT: order_agent,
    PAYMENT_AGENT: payment_agent,
    SHIPMENT_AGENT: shipment_agent,
}


async def gather_evidence(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> CaseState:
    """Coordinator step: fan out to specialists, collect findings, hand off to the policy agent."""
    state = CaseState.from_case(case)
    lock = asyncio.Lock()  # the MCP session is not safe under bursts: one call in flight

    async def run(agent: str) -> None:
        trace.emit(
            case_id=state.case_id,
            event_type="task_assigned",
            actor=COORDINATOR,
            target=agent,
            decision_code="collect_evidence",
            attributes={"tools": ",".join(sorted(AGENT_TOOLS[agent]))},
        )
        agent_gateway = AgentGateway(agent, state, gateway, trace, lock)
        finding = await SPECIALIST_RUNNERS[agent](state, agent_gateway)
        state.findings[agent] = finding
        trace.emit(
            case_id=state.case_id,
            event_type="handoff",
            actor=agent,
            target=POLICY_AGENT,
            decision_code="findings_partial" if finding.missing else "findings_ready",
            evidence_refs=finding.evidence_refs,
            attributes={"missing": ",".join(finding.missing) or None},
        )

    await asyncio.gather(*(run(agent) for agent in SPECIALIST_RUNNERS))
    return state


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3A workflow: coordinator -> specialists -> policy -> verifier."""
    state = await gather_evidence(case, gateway, trace)
    lock = asyncio.Lock()
    policy_gateway = AgentGateway(POLICY_AGENT, state, gateway, trace, lock)
    await policy_agent(state, policy_gateway)
    policy_gateway.emit(
        "handoff",
        target=VERIFIER_AGENT,
        decision_code="decision_ready",
        evidence_refs=state.decision.evidence_refs[:20] if state.decision else None,
    )
    verifier_gateway = AgentGateway(VERIFIER_AGENT, state, gateway, trace, lock)
    return verify(state, verifier_gateway, trace.contracts)
