from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway, is_transient, leaf_exceptions
from .state import require_tools
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


MAX_CASE_ATTEMPTS = 4


async def _discover_tools(settings: Settings, contracts: Contracts) -> list[str]:
    for attempt in range(1, MAX_CASE_ATTEMPTS + 1):
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                return await gateway.list_tools()
        except Exception as exc:
            if not is_transient(exc):
                raise leaf_exceptions(exc)[0] from exc
            if attempt == MAX_CASE_ATTEMPTS:
                raise RuntimeError(
                    f"tool discovery failed {attempt} times ({type(exc).__name__})"
                ) from exc
            print(f"retry tool discovery ({attempt}/{MAX_CASE_ATTEMPTS}): {type(exc).__name__}")
            await asyncio.sleep(attempt)
    raise AssertionError("unreachable")


async def _run_case(
    settings: Settings, contracts: Contracts, case: dict, trace: TraceWriter, output_root: Path
) -> None:
    """Solve one case on a fresh MCP session; retry only when the session itself dies.

    The gateway drops sessions mid-batch (ConnectError / cancelled scope), so a case is the
    retry unit: its buffered trace is rolled back on failure and never appears twice.
    """
    case_id = case["case_id"]
    for attempt in range(1, MAX_CASE_ATTEMPTS + 1):
        trace.begin()
        try:
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        except Exception as exc:
            trace.rollback()
            if not is_transient(exc):
                raise leaf_exceptions(exc)[0] from exc
            if attempt == MAX_CASE_ATTEMPTS:
                raise RuntimeError(
                    f"{case_id}: MCP session failed {attempt} times ({type(exc).__name__})"
                ) from exc
            print(f"retry {case_id} ({attempt}/{MAX_CASE_ATTEMPTS}): {type(exc).__name__}")
            await asyncio.sleep(attempt)
        except BaseException:
            trace.rollback()
            raise
        else:
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.commit()
            return


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    discovered_tools = await _discover_tools(settings, contracts)
    if not discovered_tools:
        raise RuntimeError("MCP Gateway returned no tools")
    require_tools(discovered_tools)
    for index, case_id in enumerate(case_set.case_ids, start=1):
        await _run_case(settings, contracts, case_set.cases[case_id], trace, output_root)
        print(f"[{index}/{len(case_set.case_ids)}] {case_id}", flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
