from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError

from student_agent.contracts import Contracts
from student_agent.evidence import AgentGateway
from student_agent.mcp_gateway import is_transient
from student_agent.state import CaseState, ToolNotPermitted
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
REF = "ev_" + "b" * 24
CASE = {
    "case_id": "CASE_001",
    "policy_version": "EC_POLICY_V1",
    "customer_request": {"claimed_order_id": "o-1", "claims": []},
}


class FakeGateway:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures = failures

    async def call(self, tool: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool, {"case_id": case_id, **arguments}))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("boom")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": REF,
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {"order_id": "o-1"},
        }


def make(agent: str, tmp_path: Path, gateway: FakeGateway) -> tuple[AgentGateway, CaseState, Path]:
    path = tmp_path / "trace.jsonl"
    state = CaseState.from_case(CASE)
    agent_gateway = AgentGateway(
        agent, state, gateway, TraceWriter(path, CONTRACTS), asyncio.Lock()  # type: ignore[arg-type]
    )
    return agent_gateway, state, path


def events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_call_records_evidence_and_emits_consumed_event(tmp_path: Path) -> None:
    fake = FakeGateway()
    gateway, state, path = make("order-agent", tmp_path, fake)
    record = asyncio.run(gateway.call("get_order", order_id="o-1"))
    assert record is not None and record.evidence_ref == REF
    assert fake.calls == [("get_order", {"case_id": "CASE_001", "order_id": "o-1"})]
    assert state.evidence[REF].consumed_by == "order-agent"
    [event] = events(path)
    assert event["event_type"] == "tool_result_consumed"
    assert event["tool_name"] == "get_order" and event["evidence_refs"] == [REF]


def test_tool_outside_ownership_is_rejected_before_any_mcp_call(tmp_path: Path) -> None:
    fake = FakeGateway()
    gateway, _, path = make("order-agent", tmp_path, fake)
    with pytest.raises(ToolNotPermitted):
        asyncio.run(gateway.call("get_order_payments", order_id="o-1"))
    assert fake.calls == [] and not path.exists()


def test_failed_tool_is_retried_once_then_reported_missing(tmp_path: Path) -> None:
    fake = FakeGateway(failures=2)
    gateway, state, path = make("order-agent", tmp_path, fake)
    assert asyncio.run(gateway.call("get_order", order_id="o-1")) is None
    assert len(fake.calls) == 2
    assert state.evidence == {} and not path.exists()


def test_optional_tool_is_not_retried(tmp_path: Path) -> None:
    fake = FakeGateway(failures=1)
    gateway, _, _ = make("payment-agent", tmp_path, fake)
    assert asyncio.run(gateway.call("get_refund_timeline", optional=True, order_id="o-1")) is None
    assert len(fake.calls) == 1


def test_trace_rollback_discards_events_of_interrupted_case(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    trace.begin()
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    trace.rollback()
    assert not (tmp_path / "trace.jsonl").exists()
    trace.begin()
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    trace.commit()
    assert len(events(tmp_path / "trace.jsonl")) == 1


def test_transient_classification_looks_inside_exception_groups() -> None:
    assert is_transient(BaseExceptionGroup("g", [asyncio.CancelledError()]))
    assert is_transient(ExceptionGroup("g", [MCPError(code=-32000, message="Connection closed")]))
    assert not is_transient(ExceptionGroup("g", [ValueError("logic bug")]))
    assert not is_transient(ExceptionGroup("g", [ConnectionError("x"), ValueError("y")]))
