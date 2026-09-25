from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
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
def _is_transient_mcp_error(exc: BaseException) -> bool:
    """Check whether an error is a temporary MCP/network failure."""
    if isinstance(exc, (httpx2.TransportError, TimeoutError, OSError)):
        return True

    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transient_mcp_error(child) for child in exc.exceptions)

    return False

async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")

    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"

    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    # Start each full run from clean artifacts.
    for stale in output_root.glob("*.json"):
        stale.unlink()

    trace_path.unlink(missing_ok=True)

    total = len(case_set.case_ids)
    max_case_attempts = 5

    for index, case_id in enumerate(case_set.case_ids, start=1):
        case = case_set.cases[case_id]

        success = False
        last_error: BaseException | None = None

        for attempt in range(1, max_case_attempts + 1):
            print(
                f"[{index:03d}/{total}] {case_id} "
                f"(attempt {attempt}/{max_case_attempts})",
                flush=True,
            )

            # Keep failed-attempt traces isolated so they do not pollute
            # the final observable trace.
            attempt_trace_path = (
                trace_path.parent / f".{case_id}.attempt.jsonl"
            )
            attempt_trace_path.unlink(missing_ok=True)

            attempt_trace = TraceWriter(attempt_trace_path, contracts)

            try:
                attempt_trace.emit(
                    case_id=case_id,
                    event_type="case_received",
                    actor="coordinator",
                )

                # Fresh MCP session for every attempt/case.
                async with connect_gateway(
                    settings.mcp_endpoint,
                    settings.team_api_key,
                    contracts,
                ) as gateway:
                    output = await solve_case(case, gateway, attempt_trace)

                contracts.validate_output(
                    output,
                    f"outputs/{case_id}.json",
                )

                if output.get("case_id") != case_id:
                    raise ValueError(
                        f"solver returned a mismatched case_id for {case_id}"
                    )

                attempt_trace.emit(
                    case_id=case_id,
                    event_type="case_finalized",
                    actor="coordinator",
                )

                # Write output atomically.
                target = output_root / f"{case_id}.json"
                temporary = target.with_suffix(".json.tmp")

                temporary.write_text(
                    json.dumps(
                        output,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )

                temporary.replace(target)

                # Only successful attempt traces enter final trace.jsonl.
                with trace_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        attempt_trace_path.read_text(encoding="utf-8")
                    )

                attempt_trace_path.unlink(missing_ok=True)

                print(
                    f"[{index:03d}/{total}] {case_id} OK",
                    flush=True,
                )

                success = True
                break

            except BaseException as exc:
                last_error = exc
                attempt_trace_path.unlink(missing_ok=True)

                if (
                    not _is_transient_mcp_error(exc)
                    or attempt == max_case_attempts
                ):
                    raise

                wait_seconds = attempt * 2

                print(
                    f"    MCP/network error: {type(exc).__name__}. "
                    f"Retrying in {wait_seconds}s...",
                    flush=True,
                )

                await asyncio.sleep(wait_seconds)

        if not success:
            raise RuntimeError(
                f"Failed to process {case_id}: {last_error}"
            )


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
