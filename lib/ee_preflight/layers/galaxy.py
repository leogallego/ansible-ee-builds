from __future__ import annotations

import os
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

MAX_RETRIES = 3
BACKOFF_SECONDS = [5, 15, 45]


def validate(ctx: ValidateContext) -> LayerResult:
    findings: list[Finding] = []

    reqs_path = _get_requirements_path(ctx, findings)
    if reqs_path is None:
        return LayerResult(name="galaxy", status="fail", findings=findings)

    env = _build_env(ctx)

    for attempt in range(MAX_RETRIES):
        try:
            collections_path = ctx.venv_path / "collections"
            collections_path.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [
                    "ansible-galaxy", "collection", "install",
                    "-r", str(reqs_path),
                    "-p", str(collections_path),
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
            return LayerResult(name="galaxy", status="fail", findings=findings)

        if result.returncode == 0:
            installed = _count_collections(result.stdout + result.stderr)
            findings.append(Finding(
                severity=Severity.INFO,
                message=f"{installed} collections resolved",
            ))
            return LayerResult(name="galaxy", status="pass", findings=findings)

        output = result.stdout + result.stderr

        if _is_transient(output) and attempt < MAX_RETRIES - 1:
            wait = BACKOFF_SECONDS[attempt]
            findings.append(Finding(
                severity=Severity.INFO,
                message=f"Transient error, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})",
            ))
            time.sleep(wait)
            continue

        _parse_errors(output, findings)
        return LayerResult(name="galaxy", status="fail", findings=findings)

    return LayerResult(name="galaxy", status="fail", findings=findings)


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

    tmp = ctx.venv_path.parent / "inline-requirements.yml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w") as f:
        yaml.dump({"collections": ctx.ee.galaxy.entries}, f)
    return tmp


def _build_env(ctx: ValidateContext) -> dict:
    env = os.environ.copy()
    env.pop("ANSIBLE_CONFIG", None)
    ah_token = os.environ.get("AH_TOKEN")
    if ah_token:
        env["ANSIBLE_GALAXY_SERVER_LIST"] = "automation_hub_certified,automation_hub_validated,release_galaxy"
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


def _parse_errors(output: str, findings: list[Finding]) -> None:
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

    if "No matching distribution" in output:
        for line in output.splitlines():
            if "No matching distribution" in line:
                findings.append(Finding(
                    severity=Severity.ERROR,
                    message=line.strip(),
                ))

    if not any(f.severity == Severity.ERROR for f in findings):
        last_lines = output.strip().splitlines()[-5:]
        findings.append(Finding(
            severity=Severity.ERROR,
            message="Galaxy resolution failed: " + " | ".join(l.strip() for l in last_lines if l.strip()),
        ))
