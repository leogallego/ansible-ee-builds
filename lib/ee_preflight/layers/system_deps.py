from __future__ import annotations

import re

from ..container import ContainerRuntime
from ..models import Finding, LayerResult, Severity, ValidateContext

MISSING_FILE_PATTERNS = [
    (r"fatal error: (\S+\.h): No such file or directory", "header"),
    (r"(\S+): command not found", "command"),
    (r"Package '(\S+)' not found", "pkgconfig"),
    (r"Package (\S+) was not found in the pkg-config search path", "pkgconfig"),
    (r"Cannot find (\S+)", "library"),
    (r"(libxml2|libxslt) development packages are", "devpkg"),
    (r"Failed to build '(\S+)'", "wheel"),
]


def validate(ctx: ValidateContext) -> LayerResult:
    if not ctx.container_test:
        return LayerResult(name="system_deps", status="skipped", findings=[])

    findings: list[Finding] = []

    try:
        runtime = ContainerRuntime()
    except RuntimeError as e:
        findings.append(Finding(severity=Severity.ERROR, message=str(e)))
        return LayerResult(name="system_deps", status="fail", findings=findings)

    image = ctx.ee.base_image
    findings.append(Finding(
        severity=Severity.INFO,
        message=f"Pulling base image: {image}",
    ))

    pull_result = runtime.pull(image)
    if pull_result.returncode != 0:
        findings.append(Finding(
            severity=Severity.ERROR,
            message=f"Failed to pull base image: {pull_result.stderr.strip()}",
        ))
        return LayerResult(name="system_deps", status="fail", findings=findings)

    python_version = _detect_python_version(runtime, image)

    discovered_python = _get_discovered_python(ctx)
    source_pkgs = _find_source_only_packages(discovered_python)

    if not source_pkgs:
        findings.append(Finding(
            severity=Severity.INFO,
            message="No source-only packages to test",
        ))
        return LayerResult(name="system_deps", status="pass", findings=findings)

    bindep_install = _get_bindep_install_cmd(ctx)

    for pkg in source_pkgs:
        _test_wheel_build(ctx, runtime, image, pkg, bindep_install, python_version, findings)

    status = "fail" if any(f.severity == Severity.ERROR for f in findings) else "pass"
    return LayerResult(name="system_deps", status=status, findings=findings)


def _detect_python_version(runtime: ContainerRuntime, image: str) -> str:
    result = runtime.run(
        image,
        "python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")'",
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return "3"


def _get_discovered_python(ctx: ValidateContext) -> list[str]:
    """Read Python deps from ade's discovered_requirements.txt."""
    path = ctx.venv_path / ".ansible-dev-environment" / "discovered_requirements.txt"
    if not path.exists():
        return []
    deps: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        dep = line.split("#")[0].strip()
        if dep:
            deps.append(dep)
    return deps


def _find_source_only_packages(python_deps: list[str]) -> list[str]:
    known_source_only = {
        "systemd_python", "gssapi", "ncclient", "lxml",
        "ovirt_engine_sdk_python", "python_ldap", "pynacl",
    }
    # Extras that pull in source-only deps (e.g., aiokafka[gssapi] → gssapi)
    extras_mapping = {
        "gssapi": "gssapi",
    }

    source_pkgs: list[str] = []
    seen: set[str] = set()
    for dep in python_deps:
        # Check the package name itself
        name = dep.split(">=")[0].split("==")[0].split("<")[0].split("[")[0].split(";")[0].strip()
        normalized = name.lower().replace("-", "_")
        if normalized in known_source_only and normalized not in seen:
            seen.add(normalized)
            source_pkgs.append(name)
            continue

        # Check if extras pull in source-only deps
        if "[" in dep:
            extras = dep.split("[")[1].split("]")[0].split(",")
            for extra in extras:
                extra = extra.strip().lower()
                if extra in extras_mapping and extras_mapping[extra] not in seen:
                    seen.add(extras_mapping[extra])
                    source_pkgs.append(extras_mapping[extra])

    return source_pkgs


def _get_bindep_install_cmd(ctx: ValidateContext) -> str:
    from .python_deps import read_declared_system
    declared = read_declared_system(ctx)
    pkg_names = [line.split()[0] for line in declared if line.strip()]
    if pkg_names:
        pkgmgr = ctx.ee.options.get("package_manager_path", "/usr/bin/microdnf")
        return f"{pkgmgr} install -y {' '.join(pkg_names)}"
    return "true"


def _test_wheel_build(
    ctx: ValidateContext,
    runtime: ContainerRuntime,
    image: str,
    pkg: str,
    bindep_install: str,
    python_version: str,
    findings: list[Finding],
) -> None:
    pkg_name = pkg.split(">=")[0].split("==")[0].split("<")[0].strip()
    pkgmgr = ctx.ee.options.get("package_manager_path", "/usr/bin/microdnf")
    pycmd = f"python{python_version}"

    cmd = (
        f"{bindep_install} && "
        f"{pkgmgr} install -y python3-pip python3-devel gcc 2>/dev/null; "
        f"{pycmd} -m ensurepip 2>/dev/null; "
        f"{pycmd} -m pip install --upgrade pip setuptools wheel && "
        f"{pycmd} -m pip wheel --no-binary :all: '{pkg}' -w /tmp/wheels"
    )

    result = runtime.run(image, cmd, timeout=180)

    if result.returncode == 0:
        findings.append(Finding(
            severity=Severity.INFO,
            message=f"Wheel build OK: {pkg_name}",
        ))
        return

    output = result.stdout + result.stderr
    missing_file = _extract_missing_file(output)

    if missing_file:
        pkg_provider = _find_providing_package(runtime, image, missing_file, python_version)
        if pkg_provider:
            findings.append(Finding(
                severity=Severity.ERROR,
                message=f"{pkg_name} failed to build: {missing_file} not found",
                fix=f"Add '{pkg_provider}' to bindep.txt",
                source=f"required by {pkg_name}",
            ))
        else:
            findings.append(Finding(
                severity=Severity.ERROR,
                message=f"{pkg_name} failed to build: {missing_file} not found",
                fix=f"Find the package providing {missing_file} for your base image and add it to bindep.txt",
                source=f"required by {pkg_name}",
            ))
    else:
        findings.append(Finding(
            severity=Severity.ERROR,
            message=f"{pkg_name} failed to build",
            source=f"pip wheel output: {output[-300:]}",
        ))


def _extract_missing_file(output: str) -> str | None:
    for pattern, _ in MISSING_FILE_PATTERNS:
        match = re.search(pattern, output)
        if match:
            return match.group(1)
    return None


def _find_providing_package(
    runtime: ContainerRuntime,
    image: str,
    missing_file: str,
    python_version: str,
) -> str | None:
    if missing_file == "Python.h":
        return f"python{python_version}-devel"

    # Try dnf provides inside the container (install dnf if needed)
    search = f"*/pkgconfig/{missing_file}.pc" if "." not in missing_file else f"*/{missing_file}"
    result = runtime.run(
        image,
        f"(microdnf install -y dnf 2>/dev/null || true) && "
        f"dnf provides '{search}' 2>/dev/null",
        timeout=120,
    )
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("Last") or line.startswith("=") or line.startswith("Repo"):
                continue
            match = re.match(r"^(\S+?)-\d", line)
            if match:
                return match.group(1)

    # Try apt-file for Debian-based containers
    result = runtime.run(
        image,
        f"apt-file search '{missing_file}' 2>/dev/null | head -1",
        timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        pkg = result.stdout.strip().split(":")[0]
        if pkg:
            return pkg

    return None
