from __future__ import annotations

import re

from ..container import ContainerRuntime
from ..models import Finding, LayerResult, Severity, ValidateContext

MISSING_FILE_PATTERNS = [
    (r"fatal error: (\S+\.h): No such file or directory", "header"),
    (r"(\S+): command not found", "command"),
    (r"Package (\S+) was not found in the pkg-config search path", "pkgconfig"),
    (r"Cannot find (\S+)", "library"),
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
        _test_wheel_build(runtime, image, pkg, bindep_install, python_version, findings)

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
    from .python_deps import run_introspect
    findings_ignored: list[Finding] = []
    python_deps, _ = run_introspect(ctx, findings_ignored)
    return python_deps


def _find_source_only_packages(python_deps: list[str]) -> list[str]:
    known_source_only = {
        "systemd_python", "gssapi", "ncclient", "lxml",
        "ovirt_engine_sdk_python", "python_ldap", "pynacl",
    }
    source_pkgs: list[str] = []
    for dep in python_deps:
        name = dep.split(">=")[0].split("==")[0].split("<")[0].split("[")[0].strip()
        normalized = name.lower().replace("-", "_")
        if normalized in known_source_only:
            source_pkgs.append(dep)
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
    runtime: ContainerRuntime,
    image: str,
    pkg: str,
    bindep_install: str,
    python_version: str,
    findings: list[Finding],
) -> None:
    pkg_name = pkg.split(">=")[0].split("==")[0].split("<")[0].strip()

    cmd = (
        f"{bindep_install} && "
        f"pip install --upgrade pip setuptools wheel && "
        f"pip wheel --no-binary :all: '{pkg}' -w /tmp/wheels"
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
        rpm = _find_providing_rpm(runtime, image, missing_file, python_version)
        if rpm:
            findings.append(Finding(
                severity=Severity.ERROR,
                message=f"{pkg_name} failed to build: {missing_file} not found",
                fix=f"Add '{rpm} [platform:rpm]' to bindep.txt",
                source=f"required by {pkg_name}",
            ))
        else:
            findings.append(Finding(
                severity=Severity.ERROR,
                message=f"{pkg_name} failed to build: {missing_file} not found",
                fix=f"Manually find the RPM providing {missing_file}",
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


def _find_providing_rpm(
    runtime: ContainerRuntime,
    image: str,
    missing_file: str,
    python_version: str,
) -> str | None:
    if missing_file == "Python.h":
        return f"python{python_version}-devel"

    result = runtime.run(
        image,
        f"dnf provides '*/{missing_file}' 2>/dev/null || yum provides '*/{missing_file}' 2>/dev/null",
    )
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("Last") and not line.startswith("=") and "-" in line:
                rpm_name = re.split(r"[-:]\d", line)[0]
                if rpm_name:
                    return rpm_name
    return None
