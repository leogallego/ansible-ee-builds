from __future__ import annotations

import os
import re
import shutil
import subprocess

from ..models import Finding, LayerResult, Severity, ValidateContext


def validate(ctx: ValidateContext) -> LayerResult:
    findings: list[Finding] = []

    _check_ansible_lint(ctx, findings)
    _check_file_refs(ctx, findings)
    _check_build_args(ctx, findings)
    _check_base_image(ctx, findings)

    has_missing_files = any(
        f.severity == Severity.ERROR and "not found" in f.message
        for f in findings
    )
    status = "fail" if has_missing_files else "pass"

    return LayerResult(name="prechecks", status=status, findings=findings)


def _check_ansible_lint(ctx: ValidateContext, findings: list[Finding]) -> None:
    if not shutil.which("ansible-lint"):
        findings.append(Finding(
            severity=Severity.INFO,
            message="ansible-lint not found, skipping YAML format check",
            fix="pip install ansible-lint",
        ))
        return

    try:
        result = subprocess.run(
            ["ansible-lint", str(ctx.ee.path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            for line in result.stdout.splitlines():
                if ctx.ee.path.name in line and "]" in line:
                    findings.append(Finding(
                        severity=Severity.WARNING,
                        message=f"ansible-lint: {line.strip()}",
                    ))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _check_file_refs(ctx: ValidateContext, findings: list[Finding]) -> None:
    for dep_name, dep_ref in [
        ("galaxy", ctx.ee.galaxy),
        ("python", ctx.ee.python),
        ("system", ctx.ee.system),
    ]:
        if dep_ref is None:
            continue
        if dep_ref.format.value == "file" and dep_ref.file_path:
            if not dep_ref.file_path.exists():
                findings.append(Finding(
                    severity=Severity.ERROR,
                    message=f"Dependency file not found: {dep_ref.file_path.name}",
                    fix=f"Create {dep_ref.file_path.name} in {ctx.ee.ee_dir}",
                ))


def _check_build_args(ctx: ValidateContext, findings: list[Finding]) -> None:
    for arg_name in ctx.ee.build_args:
        if not os.environ.get(arg_name):
            findings.append(Finding(
                severity=Severity.WARNING,
                message=f"ARG {arg_name} declared in build steps but ${arg_name} is not set",
                fix=f"export {arg_name}=<value> before running",
            ))


def _check_base_image(ctx: ValidateContext, findings: list[Finding]) -> None:
    image = ctx.ee.base_image
    if not image:
        findings.append(Finding(
            severity=Severity.ERROR,
            message="No base image specified",
        ))
        return

    if not re.match(r"^[\w.\-]+(/[\w.\-]+)+(:\S+)?(@sha256:[a-f0-9]+)?$", image):
        findings.append(Finding(
            severity=Severity.WARNING,
            message=f"Base image may be malformed: {image}",
        ))

    if "@sha256:" in image:
        findings.append(Finding(
            severity=Severity.INFO,
            message="Base image uses SHA digest pin",
        ))
