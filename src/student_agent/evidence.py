"""Per-agent view of the MCP gateway.

Every MCP call made by an agent goes through `AgentGateway`, which
- rejects tools the agent does not own (least privilege),
- always sends the state's own `case_id` (evidence can never cross cases),
- serializes calls on one session (the gateway drops sessions under bursts),
- records the untouched envelope in `CaseState`, and
- emits `tool_result_consumed` for each evidence actually received.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import AGENT_TOOLS, CaseState, EvidenceRecord, ToolNotPermitted
from .trace import TraceWriter

CALL_TIMEOUT_SECONDS = 60.0
TOOL_ATTEMPTS = 2  # one retry; MCP reads are idempotent


class AgentGateway:
    def __init__(
        self,
        agent: str,
        state: CaseState,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        lock: asyncio.Lock,
    ) -> None:
        self.agent = agent
        self._state = state
        self._gateway = gateway
        self._trace = trace
        self._lock = lock

    def emit(self, event_type: str, **fields: Any) -> None:
        """Emit a trace event as this agent, always scoped to this case."""
        self._trace.emit(
            case_id=self._state.case_id, event_type=event_type, actor=self.agent, **fields
        )

    async def call(
        self, tool: str, *, optional: bool = False, **arguments: str
    ) -> EvidenceRecord | None:
        """Call `tool`; return the recorded evidence, or None if the tool has no data.

        `optional=True` marks tools whose failure legitimately means "no such data"
        (e.g. refund timeline of an order that was never refunded): no retry, no error.
        Session-level failures (timeouts, cancelled scope) propagate to the caller so the
        whole case is retried on a fresh session.
        """
        if tool not in AGENT_TOOLS.get(self.agent, frozenset()):
            raise ToolNotPermitted(f"{self.agent} may not call {tool}")
        attempts = 1 if optional else TOOL_ATTEMPTS
        envelope: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self._lock:
                    envelope = await asyncio.wait_for(
                        self._gateway.call(tool, case_id=self._state.case_id, **arguments),
                        CALL_TIMEOUT_SECONDS,
                    )
                break
            except RuntimeError:
                if attempt == attempts:
                    return None
        assert envelope is not None
        record = self._state.record(tool, envelope, self.agent)
        self._trace.emit(
            case_id=self._state.case_id,
            event_type="tool_result_consumed",
            actor=self.agent,
            tool_name=tool,
            evidence_refs=[record.evidence_ref],
            attributes={"domain": record.domain},
        )
        return record
