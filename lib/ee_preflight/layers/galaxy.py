from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import yaml

from ..models import DepFormat, Finding, LayerResult, Severity, ValidateContext

TRANSIENT_PATTERNS = [
    "HTTP Error 504",
    "HTTP Error 502",
    "HTTP Error 429",
    "Connection timed out",
    "Connection refused",
    "Gateway Time-out",
]

PYTHON_BUILD_PATTERNS = [
    "No module named",
    "command not found",
    "No such file or directory",
    "Failed building wheel",
    "Failed to build",
    "pkg-config search path",
    "Cannot find",
]

MAX_RETRIES = 3
BACKOFF_SECONDS = [5, 15, 45]


def validate(ctx: ValidateContext) -> tuple[LayerResult, list[Finding]]:
    """Returns (layer1_result, python_build_findings).

    Python build findings are separated so the runner can attach them
    to Layer 2 instead of failing Layer 1.
    """
    findings: list[Finding] = []
    python_findings: list[Finding] = []

    reqs_path = _get_requirements_path(ctx, findings)
    if reqs_path is None:
        return LayerResult(name="galaxy", status="fail", findings=findings), []

    env = _build_env(ctx)

    for attempt in range(MAX_RETRIES):
        try:
            result = subprocess.run(
                [
                    "ade", "install",
                    "-r", str(reqs_path),
                    "--venv", str(ctx.venv_path),
                    "--im", "none",
                    "-v",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            findings.append(Finding(
                severity=Severity.ERROR,
                message="Galaxy resolution timed out after 600s",
            ))
            return LayerResult(name="galaxy", status="fail", findings=findings), []

        output = result.stdout + result.stderr

        if result.returncode == 0:
            installed = _count_collections(output)
            findings.append(Finding(
                severity=Severity.INFO,
                message=f"{installed} collections resolved and installed",
            ))
            return LayerResult(name="galaxy", status="pass", findings=findings), []

        if _is_transient(output) and attempt < MAX_RETRIES - 1:
            wait = BACKOFF_SECONDS[attempt]
            findings.append(Finding(
                severity=Severity.INFO,
                message=f"Transient error, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})",
            ))
            time.sleep(wait)
            continue

        # Separate collection errors from Python build failures
        collection_errors = _parse_collection_errors(output)
        python_build_errors = _parse_python_build_errors(output)

        if collection_errors:
            findings.extend(collection_errors)
            return LayerResult(name="galaxy", status="fail", findings=findings), []

        if python_build_errors:
            # Collections resolved but Python deps failed to build —
            # that's a Layer 2 finding, not a Layer 1 failure
            installed = _count_collections(output)
            findings.append(Finding(
                severity=Severity.INFO,
                message=f"{installed} collections resolved (Python dep build issues detected)",
            ))
            return (
                LayerResult(name="galaxy", status="pass", findings=findings),
                python_build_errors,
            )

        # Unknown failure
        last_lines = output.strip().splitlines()[-5:]
        findings.append(Finding(
            severity=Severity.ERROR,
            message="Galaxy resolution failed: " + " | ".join(
                l.strip() for l in last_lines if l.strip()
            ),
        ))
        return LayerResult(name="galaxy", status="fail", findings=findings), []

    return LayerResult(name="galaxy", status="fail", findings=findings), []


def _get_requirements_path(
    ctx: ValidateContext, findings: list[Finding]
) -> Path | None:
    if ctx.ee.galaxy is None:
        findings.append(Finding(
            severity=Severity.ERROR,
            message="No galaxy dependencies defined",
        ))
        return None

    if ctx.ee.galaxy.format == DepFormat.FILE:
        return ctx.ee.galaxy.file_path

    tmp_dir = Path("tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / "inline-requirements.yml"
    with open(tmp, "w") as f:
        yaml.dump({"collections": ctx.ee.galaxy.entries}, f)
    return tmp


def _build_env(ctx: ValidateContext) -> dict:
    env = os.environ.copy()
    env.pop("ANSIBLE_CONFIG", None)
    # Keep uv cache local to avoid read-only filesystem issues in sandboxed environments
    env.setdefault("UV_CACHE_DIR", str(Path("tmp") / "uv-cache"))
    ah_token = os.environ.get("AH_TOKEN")
    if ah_token:
        env["ANSIBLE_GALAXY_SERVER_LIST"] = (
            "automation_hub_certified,automation_hub_validated,release_galaxy"
        )
        env["ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_CERTIFIED_URL"] = (
            "https://console.redhat.com/api/automation-hub/content/published/"
        )
        env["ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_CERTIFIED_AUTH_URL"] = (
            "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
        )
        env["ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_CERTIFIED_TOKEN"] = ah_token
        env["ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_VALIDATED_URL"] = (
            "https://console.redhat.com/api/automation-hub/content/validated/"
        )
        env["ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_VALIDATED_AUTH_URL"] = (
            "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
        )
        env["ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_VALIDATED_TOKEN"] = ah_token
        env["ANSIBLE_GALAXY_SERVER_RELEASE_GALAXY_URL"] = "https://galaxy.ansible.com/"

    return env


def _is_transient(output: str) -> bool:
    return any(p in output for p in TRANSIENT_PATTERNS)


def _count_collections(output: str) -> int:
    count = output.count("was installed successfully")
    if count == 0:
        count = output.count("Installing ")
    return count


def _parse_collection_errors(output: str) -> list[Finding]:
    findings: list[Finding] = []

    if "Could not satisfy" in output or "Failed to resolve" in output:
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("*"):
                findings.append(Finding(
                    severity=Severity.ERROR,
                    message=f"Collection conflict: {line[2:]}",
                ))

    if any(p in output for p in ("HTTP Error 400", "Unauthorized", "HTTP Error 401")):
        findings.append(Finding(
            severity=Severity.ERROR,
            message="Galaxy/Automation Hub authentication failed",
            fix="Check AH_TOKEN or ansible.cfg credentials",
        ))

    return findings


def _parse_python_build_errors(output: str) -> list[Finding]:
    findings: list[Finding] = []

    if not any(p in output for p in PYTHON_BUILD_PATTERNS):
        return findings

    missing_file_patterns = [
        (r"fatal error: (\S+\.h): No such file or directory", "header"),
        (r"(\S+): command not found", "command"),
        (r"Package '(\S+)' not found", "pkgconfig"),
        (r"Package (\S+) was not found in the pkg-config search path", "pkgconfig"),
    ]

    seen: set[str] = set()
    for pattern, kind in missing_file_patterns:
        for match in re.finditer(pattern, output):
            missing = match.group(1)
            if missing in seen:
                continue
            seen.add(missing)

            rpm = _resolve_rpm(missing, kind)
            if rpm:
                findings.append(Finding(
                    severity=Severity.WARNING,
                    message=f"Python dep build failed: {missing} not found ({kind})",
                    fix=f"Add '{rpm} [platform:rpm]' to bindep.txt",
                    source="detected during ade install (Python dep compilation)",
                ))
            else:
                findings.append(Finding(
                    severity=Severity.WARNING,
                    message=f"Python dep build failed: {missing} not found ({kind})",
                    fix=f"Find the RPM providing {missing} and add it to bindep.txt",
                    source="detected during ade install (Python dep compilation)",
                ))

    if not findings:
        for line in output.splitlines():
            if "Failed to build" in line or "Failed building wheel" in line:
                pkg = line.strip().split("'")[1] if "'" in line else "unknown"
                findings.append(Finding(
                    severity=Severity.WARNING,
                    message=f"Python dep failed to build: {pkg}",
                    fix="Check system -devel packages required for compilation",
                    source="detected during ade install",
                ))

    return findings


def _resolve_rpm(missing: str, kind: str) -> str | None:
    """Try to resolve the missing file/command to an RPM package name."""
    if missing == "Python.h":
        return "python3-devel"

    search = f"*/{missing}" if kind == "header" else f"*/{missing}"
    if kind == "pkgconfig":
        search = f"*/pkgconfig/{missing}.pc"

    try:
        result = subprocess.run(
            ["dnf", "provides", search],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("Last") or line.startswith("="):
                    continue
                match = re.match(r"^(\S+?)-\d", line)
                if match:
                    return match.group(1)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None
