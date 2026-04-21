from __future__ import annotations

import subprocess
from pathlib import Path

from ..models import DepFormat, Finding, LayerResult, Severity, ValidateContext


def validate(ctx: ValidateContext) -> LayerResult:
    findings: list[Finding] = []

    _run_ade_check(ctx, findings)
    discovered_python, discovered_system = run_introspect(ctx, findings)
    _diff_python_deps(ctx, discovered_python, findings)
    _diff_system_deps(ctx, discovered_system, findings)

    status = "fail" if any(f.severity == Severity.ERROR for f in findings) else "pass"
    return LayerResult(name="python_deps", status=status, findings=findings)


def _run_ade_check(ctx: ValidateContext, findings: list[Finding]) -> None:
    try:
        result = subprocess.run(
            ["ade", "check", "--venv", str(ctx.venv_path), "-v"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            output = result.stdout + result.stderr
            for line in output.splitlines():
                line = line.strip()
                if "missing" in line.lower() or "conflict" in line.lower():
                    findings.append(Finding(
                        severity=Severity.ERROR,
                        message=f"ade check: {line}",
                    ))
    except FileNotFoundError:
        findings.append(Finding(
            severity=Severity.ERROR,
            message="ade not found",
            fix="pip install ansible-dev-environment",
        ))
    except subprocess.TimeoutExpired:
        findings.append(Finding(
            severity=Severity.WARNING,
            message="ade check timed out after 120s",
        ))


def run_introspect(
    ctx: ValidateContext, findings: list[Finding]
) -> tuple[list[str], list[str]]:
    discovered_python: list[str] = []
    discovered_system: list[str] = []

    site_packages = _find_site_packages(ctx.venv_path)
    if not site_packages:
        findings.append(Finding(
            severity=Severity.WARNING,
            message="Could not locate venv site-packages for introspection",
        ))
        return discovered_python, discovered_system

    try:
        result = subprocess.run(
            ["ansible-builder", "introspect", str(site_packages)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            discovered_python, discovered_system = _parse_introspect(result.stdout)
    except FileNotFoundError:
        findings.append(Finding(
            severity=Severity.ERROR,
            message="ansible-builder not found",
            fix="pip install ansible-builder",
        ))
    except subprocess.TimeoutExpired:
        findings.append(Finding(
            severity=Severity.WARNING,
            message="ansible-builder introspect timed out",
        ))

    return discovered_python, discovered_system


def _find_site_packages(venv_path: Path) -> Path | None:
    lib_dir = venv_path / "lib"
    if not lib_dir.exists():
        return None
    for pydir in sorted(lib_dir.iterdir(), reverse=True):
        sp = pydir / "site-packages"
        if sp.exists():
            return sp
    return None


def _parse_introspect(output: str) -> tuple[list[str], list[str]]:
    python_deps: list[str] = []
    system_deps: list[str] = []
    section = None

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("python:"):
            section = "python"
            continue
        if stripped.startswith("system:"):
            section = "system"
            continue
        if not stripped or stripped.startswith("#"):
            continue

        if section == "python" and stripped.startswith("- "):
            dep = stripped[2:].strip().strip("'\"")
            dep = dep.split("#")[0].strip()
            if dep:
                python_deps.append(dep)
        elif section == "system" and stripped.startswith("- "):
            dep = stripped[2:].strip().strip("'\"")
            dep = dep.split("#")[0].strip()
            if dep:
                system_deps.append(dep)

    return python_deps, system_deps


def _diff_python_deps(
    ctx: ValidateContext,
    discovered: list[str],
    findings: list[Finding],
) -> None:
    declared = read_declared_python(ctx)
    declared_names = {_pkg_name(d) for d in declared}

    for dep in discovered:
        name = _pkg_name(dep)
        if name and name not in declared_names:
            findings.append(Finding(
                severity=Severity.WARNING,
                message=f"Undeclared Python dep: {dep}",
                fix=f"Add '{dep}' to python requirements",
                source="discovered by ansible-builder introspect",
            ))


def _diff_system_deps(
    ctx: ValidateContext,
    discovered: list[str],
    findings: list[Finding],
) -> None:
    declared = read_declared_system(ctx)
    declared_names = {line.split()[0] for line in declared if line.strip()}

    for dep in discovered:
        pkg_name = dep.split()[0]
        if pkg_name not in declared_names:
            bindep_entry = dep if "[" in dep else f"{dep} [platform:rpm]"
            findings.append(Finding(
                severity=Severity.WARNING,
                message=f"Undeclared system dep: {pkg_name}",
                fix=f"Add '{bindep_entry}' to bindep.txt",
                source="discovered by ansible-builder introspect",
            ))


def read_declared_python(ctx: ValidateContext) -> list[str]:
    if ctx.ee.python is None:
        return []
    if ctx.ee.python.format == DepFormat.FILE and ctx.ee.python.file_path:
        if ctx.ee.python.file_path.exists():
            return [
                line.strip()
                for line in ctx.ee.python.file_path.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
    if ctx.ee.python.format == DepFormat.INLINE:
        return [str(e) for e in ctx.ee.python.entries]
    return []


def read_declared_system(ctx: ValidateContext) -> list[str]:
    if ctx.ee.system is None:
        return []
    if ctx.ee.system.format == DepFormat.FILE and ctx.ee.system.file_path:
        if ctx.ee.system.file_path.exists():
            return [
                line.strip()
                for line in ctx.ee.system.file_path.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
    if ctx.ee.system.format == DepFormat.INLINE:
        return [str(e) for e in ctx.ee.system.entries]
    return []


def _pkg_name(spec: str) -> str:
    for sep in (">=", "<=", "==", "!=", ">", "<", "[", ";"):
        spec = spec.split(sep)[0]
    return spec.strip().lower().replace("-", "_")
