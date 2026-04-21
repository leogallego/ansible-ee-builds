from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .ee_parser import parse_ee
from .fixer import apply_fixes
from .layers import galaxy, prechecks, python_deps, system_deps
from .models import Finding, LayerResult, Severity, ValidateContext


def run(
    ee_path: Path,
    fix: bool = False,
    build: bool = False,
    tag: str | None = None,
    venv_path: Path | None = None,
    keep_venv: bool = False,
    container_test: bool = False,
    verbose: bool = False,
) -> list[LayerResult]:
    ee = parse_ee(ee_path)
    user_venv = venv_path is not None

    if venv_path is None:
        path_hash = hashlib.md5(str(ee_path).encode()).hexdigest()[:8]
        venv_path = Path("tmp") / f"ee-preflight-{path_hash}"

    if not (venv_path / "bin" / "python").exists():
        venv_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_path)],
            check=True,
        )

    ctx = ValidateContext(
        ee=ee,
        venv_path=venv_path,
        fix=fix,
        container_test=container_test,
        verbose=verbose,
    )

    results: list[LayerResult] = []

    try:
        r0 = prechecks.validate(ctx)
        results.append(r0)
        missing_files = any(
            f.severity == Severity.ERROR and "not found" in f.message
            for f in r0.findings
        )

        if missing_files:
            results.extend([
                LayerResult(name="galaxy", status="skipped"),
                LayerResult(name="python_deps", status="skipped"),
                LayerResult(name="system_deps", status="skipped"),
            ])
            return results

        r1 = galaxy.validate(ctx)
        results.append(r1)

        if r1.has_errors:
            results.extend([
                LayerResult(name="python_deps", status="skipped"),
                LayerResult(name="system_deps", status="skipped"),
            ])
        else:
            r2 = python_deps.validate(ctx)
            results.append(r2)

            r3 = system_deps.validate(ctx)
            results.append(r3)

        if fix:
            all_findings = [f for r in results for f in r.findings]
            fixable = [
                f for f in all_findings
                if f.fix and f.severity == Severity.WARNING
            ]
            if fixable:
                changes = apply_fixes(ee, fixable)
                for change in changes:
                    results.append(LayerResult(
                        name="fix",
                        status="pass",
                        findings=[Finding(severity=Severity.INFO, message=change)],
                    ))

        if build:
            has_errors = any(r.has_errors for r in results)
            if has_errors:
                results.append(LayerResult(
                    name="build",
                    status="skipped",
                    findings=[Finding(
                        severity=Severity.ERROR,
                        message="Build skipped: unresolved errors remain",
                    )],
                ))
            else:
                build_result = _run_build(ee, tag)
                results.append(build_result)

    finally:
        if not user_venv and not keep_venv:
            shutil.rmtree(venv_path, ignore_errors=True)

    return results


def _run_build(ee, tag: str | None) -> LayerResult:
    ee_name = ee.ee_dir.name
    if tag is None:
        tag = f"{ee_name}:latest"

    cmd = [
        "ansible-builder", "build",
        "-f", str(ee.path),
        "-t", tag,
        "-v", "3",
    ]

    for arg_name in ee.build_args:
        val = os.environ.get(arg_name)
        if val:
            cmd.extend(["--build-arg", f"{arg_name}={val}"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return LayerResult(
            name="build",
            status="fail",
            findings=[Finding(
                severity=Severity.ERROR,
                message="Build timed out after 600s",
            )],
        )

    if result.returncode == 0:
        return LayerResult(
            name="build",
            status="pass",
            findings=[Finding(
                severity=Severity.INFO,
                message=f"Image built successfully: {tag}",
            )],
        )

    return LayerResult(
        name="build",
        status="fail",
        findings=[Finding(
            severity=Severity.ERROR,
            message=f"Build failed: {result.stderr[-300:]}",
        )],
    )
