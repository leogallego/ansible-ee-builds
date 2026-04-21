from __future__ import annotations

import re
from pathlib import Path

from ..models import DepFormat, Finding, LayerResult, Severity, ValidateContext

ADE_ENV_DIR = ".ansible-dev-environment"
DISCOVERED_PYTHON = "discovered_requirements.txt"
DISCOVERED_SYSTEM = "discovered_bindep.txt"


def validate(ctx: ValidateContext) -> LayerResult:
    findings: list[Finding] = []

    discovered_python = _read_discovered_python(ctx)
    discovered_system = _read_discovered_system(ctx)

    if not discovered_python and not discovered_system:
        findings.append(Finding(
            severity=Severity.INFO,
            message="No discovered dependencies found (ade may not have completed introspection)",
        ))
        return LayerResult(name="python_deps", status="pass", findings=findings)

    _diff_python_deps(ctx, discovered_python, findings)
    _diff_system_deps(ctx, discovered_system, findings)

    status = "fail" if any(f.severity == Severity.ERROR for f in findings) else "pass"
    return LayerResult(name="python_deps", status=status, findings=findings)


def _read_discovered_python(ctx: ValidateContext) -> list[dict]:
    """Read ade's discovered_requirements.txt, return list of {dep, source}."""
    path = ctx.venv_path / ADE_ENV_DIR / DISCOVERED_PYTHON
    if not path.exists():
        return []

    entries: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        source = None
        if "# from collection" in line:
            parts = line.split("# from collection")
            line = parts[0].strip()
            source = parts[1].strip()
        entries.append({"dep": line, "source": source})
    return entries


def _read_discovered_system(ctx: ValidateContext) -> list[dict]:
    """Read ade's discovered_bindep.txt, return list of {dep, source, platforms}."""
    path = ctx.venv_path / ADE_ENV_DIR / DISCOVERED_SYSTEM
    if not path.exists():
        return []

    entries: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        source = None
        if "# from collection" in line:
            parts = line.split("# from collection")
            line = parts[0].strip()
            source = parts[1].strip()

        pkg_name = line.split()[0]
        platforms = re.findall(r"platform:(\S+)", line)
        entries.append({
            "dep": line,
            "pkg_name": pkg_name,
            "source": source,
            "platforms": platforms,
        })
    return entries


def _diff_python_deps(
    ctx: ValidateContext,
    discovered: list[dict],
    findings: list[Finding],
) -> None:
    declared = _read_declared_python(ctx)
    declared_names = {_pkg_name(d) for d in declared}

    seen: set[str] = set()
    for entry in discovered:
        name = _pkg_name(entry["dep"])
        if not name or name in declared_names or name in seen:
            continue
        seen.add(name)

        findings.append(Finding(
            severity=Severity.INFO,
            message=f"Transitive Python dep: {entry['dep']}",
            source=f"from collection {entry['source']}" if entry["source"] else None,
        ))


def _diff_system_deps(
    ctx: ValidateContext,
    discovered: list[dict],
    findings: list[Finding],
) -> None:
    declared = _read_declared_system(ctx)
    declared_names = {line.split()[0] for line in declared if line.strip()}

    target_platform = _detect_target_platform(ctx)

    seen: set[str] = set()
    for entry in discovered:
        pkg_name = entry["pkg_name"]
        platforms = entry["platforms"]

        if pkg_name in declared_names or pkg_name in seen:
            continue

        if platforms and not _matches_platform(platforms, target_platform):
            continue

        seen.add(pkg_name)

        findings.append(Finding(
            severity=Severity.WARNING,
            message=f"Undeclared system dep: {pkg_name}",
            fix=f"Add '{entry['dep']}' to bindep.txt",
            source=f"from collection {entry['source']}" if entry["source"] else None,
        ))


def _detect_target_platform(ctx: ValidateContext) -> str:
    """Infer the target platform from the base image name."""
    image = ctx.ee.base_image.lower()
    if "rhel-9" in image or "rhel9" in image:
        return "rhel-9"
    if "rhel-8" in image or "rhel8" in image:
        return "rhel-8"
    if "centos-9" in image or "centos9" in image:
        return "centos-9"
    if "centos-8" in image or "centos8" in image:
        return "centos-8"
    if "fedora" in image:
        return "fedora"
    if "debian" in image or "ubuntu" in image:
        return "debian"
    return "rpm"


def _matches_platform(platforms: list[str], target: str) -> bool:
    """Check if any of the bindep platform tags match our target."""
    for p in platforms:
        if p == target:
            return True
        if p == "rpm" and target in ("rhel-8", "rhel-9", "centos-8", "centos-9", "fedora"):
            return True
        if p == "redhat" and target in ("rhel-8", "rhel-9", "centos-8", "centos-9"):
            return True
        if p == "dpkg" and target in ("debian", "ubuntu"):
            return True
    return False


def read_declared_python(ctx: ValidateContext) -> list[str]:
    return _read_declared_python(ctx)


def read_declared_system(ctx: ValidateContext) -> list[str]:
    return _read_declared_system(ctx)


def _read_declared_python(ctx: ValidateContext) -> list[str]:
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


def _read_declared_system(ctx: ValidateContext) -> list[str]:
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
