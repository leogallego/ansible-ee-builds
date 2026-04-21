from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import LayerResult, Severity
from .runner import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ee-preflight",
        description="Pre-build validation for Ansible Execution Environments",
    )
    parser.add_argument("ee_path", type=Path, help="Path to execution-environment.yml")
    parser.add_argument("--fix", action="store_true", help="Auto-fix missing deps")
    parser.add_argument("--build", action="store_true", help="Run ansible-builder after validation")
    parser.add_argument("--tag", help="Image tag for --build (default: <name>:latest)")
    parser.add_argument("--venv", type=Path, dest="venv_path", help="Venv path (kept after run)")
    parser.add_argument("--keep-venv", action="store_true", help="Keep temp venv after run")
    parser.add_argument("--container-test", action="store_true", help="Test wheel builds in container")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output JSON")
    parser.add_argument("--verbose", action="store_true", help="Show passing checks")

    args = parser.parse_args()

    if not args.ee_path.exists():
        print(f"Error: {args.ee_path} not found", file=sys.stderr)
        sys.exit(1)

    results = run(
        ee_path=args.ee_path,
        fix=args.fix,
        build=args.build,
        tag=args.tag,
        venv_path=args.venv_path,
        keep_venv=args.keep_venv,
        container_test=args.container_test,
        verbose=args.verbose,
    )

    if args.json_output:
        _output_json(args.ee_path, results)
    else:
        _output_human(args.ee_path, results, args.verbose)

    has_errors = any(r.has_errors for r in results)
    sys.exit(1 if has_errors else 0)


LAYER_NAMES = {
    "prechecks": "Layer 0: Pre-checks",
    "galaxy": "Layer 1: Galaxy Resolution",
    "python_deps": "Layer 2: Dependency Validation",
    "system_deps": "Layer 3: Container Wheel Test",
    "fix": "Fix",
    "build": "Build",
}


def _output_json(ee_path: Path, results: list[LayerResult]) -> None:
    overall = "fail" if any(r.has_errors for r in results) else "pass"
    output = {
        "ee": str(ee_path),
        "result": overall,
        "layers": [r.to_dict() for r in results],
    }
    print(json.dumps(output, indent=2))


def _output_human(
    ee_path: Path, results: list[LayerResult], verbose: bool
) -> None:
    print(f"\nee-preflight: {ee_path}\n")

    errors = 0
    warnings = 0

    for result in results:
        label = LAYER_NAMES.get(result.name, result.name)

        if result.status == "skipped":
            print(f"{label} (skipped)")
            continue

        icon = "✓" if result.status == "pass" else "✗"
        print(f"{label} {icon}")

        for finding in result.findings:
            if finding.severity == Severity.INFO and not verbose:
                continue
            if finding.severity == Severity.ERROR:
                print(f"  ✗ {finding.message}")
                errors += 1
            elif finding.severity == Severity.WARNING:
                print(f"  ⚠ {finding.message}")
                warnings += 1
            else:
                print(f"  ℹ {finding.message}")

            if finding.fix:
                print(f"    → {finding.fix}")
            if finding.source:
                print(f"    ({finding.source})")

        print()

    overall = "PASS" if errors == 0 else "FAIL"
    print(f"Result: {overall} ({errors} error(s), {warnings} warning(s))")
