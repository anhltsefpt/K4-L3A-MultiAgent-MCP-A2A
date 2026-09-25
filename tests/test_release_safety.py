"""The repository must never carry competition payload or credentials.

Running the competition downloads `case-set.json`, `inputs/` and writes `outputs/`
into the working tree, so the guarantee we can actually hold is about what git
tracks. Anything the payload touches has to stay ignored.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PAYLOAD_PATHS = ("case-set.json", "inputs", "outputs", "traces", "dist", ".env")


def _tracked_files() -> set[str] | None:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return set(result.stdout.split())


def test_repository_tracks_no_competition_payload() -> None:
    tracked = _tracked_files()
    if tracked is None:
        # Not a git checkout: fall back to the released-artifact guarantee.
        assert not (ROOT / "case-set.json").exists()
        return
    offenders = sorted(
        path
        for path in tracked
        if path == "case-set.json"
        or any(path.startswith(f"{prefix}/") and not path.endswith(".gitkeep")
               for prefix in ("inputs", "outputs", "traces", "dist"))
    )
    assert offenders == [], f"competition payload is tracked by git: {offenders}"


def test_repository_tracks_no_environment_file() -> None:
    tracked = _tracked_files()
    if tracked is None:
        return
    assert ".env" not in tracked


def test_payload_paths_are_ignored() -> None:
    ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for prefix in PAYLOAD_PATHS:
        assert prefix in ignore_rules, f"{prefix} must be listed in .gitignore"


def test_repository_contains_no_oracle_or_reference_output() -> None:
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in ROOT.rglob("*"))


def test_example_environment_has_no_real_key() -> None:
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
