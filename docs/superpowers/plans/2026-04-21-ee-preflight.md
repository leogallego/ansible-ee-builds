# ee-preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI tool that validates Ansible Execution Environment definitions before running ansible-builder, catching dependency conflicts, missing system packages, and auth issues in one pass.

**Architecture:** Python CLI with 4 validation layers (prechecks → galaxy → python/system deps → container wheel test), an EE parser supporting both file-ref and inline dep formats, a fixer for `--fix`, and a runner that orchestrates everything. Each layer returns findings; the runner decides whether to proceed.

**Tech Stack:** Python 3.10+, ade, podman, ansible-lint (optional), PyYAML

**Status (2026-04-21):** Tasks 1-8 implemented. Layer 1 uses `ade install` (not `ansible-galaxy`). Layer 2 uses ade's discovered deps (not `ansible-builder introspect`). 4/7 netbox failures caught. Next: auto-trigger Layer 3 when Layer 2 finds Python build failures (catches remaining 3/7).

---

### Task 1: Data models and EE parser

**Files:**
- Create: `lib/ee_preflight/__init__.py`
- Create: `lib/ee_preflight/models.py`
- Create: `lib/ee_preflight/ee_parser.py`

- [ ] **Step 1: Create package and models**

```python
# lib/ee_preflight/__init__.py
# (empty)

# lib/ee_preflight/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Finding:
    severity: Severity
    message: str
    fix: str | None = None
    source: str | None = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "message": self.message,
            "fix": self.fix,
            "source": self.source,
        }


@dataclass
class LayerResult:
    name: str
    status: str  # "pass", "fail", "skipped"
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(f.severity == Severity.ERROR for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
        }


class DepFormat(Enum):
    FILE = "file"
    INLINE = "inline"


@dataclass
class DepRef:
    format: DepFormat
    file_path: Path | None = None  # for FILE format
    entries: list = field(default_factory=list)  # for INLINE format


@dataclass
class EEDefinition:
    path: Path
    ee_dir: Path
    version: int
    base_image: str
    galaxy: DepRef | None = None
    python: DepRef | None = None
    system: DepRef | None = None
    build_steps: dict = field(default_factory=dict)
    build_files: list = field(default_factory=list)
    options: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def build_args(self) -> list[str]:
        args = []
        for step_list in self.build_steps.values():
            for step in step_list:
                if step.strip().startswith("ARG "):
                    arg_name = step.strip().split()[1].split("=")[0]
                    args.append(arg_name)
        return args


@dataclass
class ValidateContext:
    ee: EEDefinition
    venv_path: Path
    fix: bool = False
    container_test: bool = False
    verbose: bool = False
```

- [ ] **Step 2: Create EE parser**

```python
# lib/ee_preflight/ee_parser.py
from __future__ import annotations
from pathlib import Path

import yaml

from .models import DepFormat, DepRef, EEDefinition


def parse_ee(ee_path: Path) -> EEDefinition:
    ee_path = ee_path.resolve()
    ee_dir = ee_path.parent

    with open(ee_path) as f:
        raw = yaml.safe_load(f)

    version = raw.get("version", 1)
    base_image = _extract_base_image(raw, version)
    galaxy = _parse_dep(raw, "galaxy", ee_dir)
    python = _parse_dep(raw, "python", ee_dir)
    system = _parse_dep(raw, "system", ee_dir)
    build_steps = raw.get("additional_build_steps", {})
    build_files = raw.get("additional_build_files", [])
    options = raw.get("options", {})

    return EEDefinition(
        path=ee_path,
        ee_dir=ee_dir,
        version=version,
        base_image=base_image,
        galaxy=galaxy,
        python=python,
        system=system,
        build_steps=build_steps,
        build_files=build_files,
        options=options,
        raw=raw,
    )


def _extract_base_image(raw: dict, version: int) -> str:
    if version >= 3:
        return raw.get("images", {}).get("base_image", {}).get("name", "")
    return raw.get("build_arg_defaults", {}).get("EE_BASE_IMAGE", "")


def _parse_dep(raw: dict, dep_type: str, ee_dir: Path) -> DepRef | None:
    deps = raw.get("dependencies", {})
    value = deps.get(dep_type)

    if value is None:
        return None

    if isinstance(value, str):
        return DepRef(
            format=DepFormat.FILE,
            file_path=(ee_dir / value).resolve(),
        )

    if isinstance(value, list):
        return DepRef(format=DepFormat.INLINE, entries=value)

    if isinstance(value, dict):
        if "collections" in value:
            return DepRef(format=DepFormat.INLINE, entries=value["collections"])
        return DepRef(format=DepFormat.INLINE, entries=[])

    return None
```

- [ ] **Step 3: Commit**

```bash
git add lib/ee_preflight/
git commit -m "Add data models and EE parser with dual format support"
```

---

### Task 2: Layer 0 — Pre-checks

**Files:**
- Create: `lib/ee_preflight/layers/__init__.py`
- Create: `lib/ee_preflight/layers/prechecks.py`

- [ ] **Step 1: Create Layer 0**

```python
# lib/ee_preflight/layers/__init__.py
# (empty)

# lib/ee_preflight/layers/prechecks.py
from __future__ import annotations
import os
import re
import shutil
import subprocess
from pathlib import Path

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
    status = "fail" if has_missing_files else ("pass" if not findings else "pass")

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
            message="Base image uses SHA digest pin — reproducible but won't get updates",
        ))
```

- [ ] **Step 2: Commit**

```bash
git add lib/ee_preflight/layers/
git commit -m "Add Layer 0: pre-checks (ansible-lint, file refs, ARG/env vars)"
```

---

### Task 3: Layer 1 — Galaxy Resolution

**Files:**
- Create: `lib/ee_preflight/layers/galaxy.py`

- [ ] **Step 1: Create Layer 1**

```python
# lib/ee_preflight/layers/galaxy.py
from __future__ import annotations
import os
import subprocess
import tempfile
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
        result = subprocess.run(
            [
                "ade",
                "install",
                "-r", str(reqs_path),
                "--venv", str(ctx.venv_path),
                "-v",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )

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

    # Inline: write to temp file for ade
    tmp = ctx.venv_path.parent / "inline-requirements.yml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w") as f:
        yaml.dump({"collections": ctx.ee.galaxy.entries}, f)
    return tmp


def _build_env(ctx: ValidateContext) -> dict:
    env = os.environ.copy()
    ah_token = os.environ.get("AH_TOKEN")
    if ah_token:
        env["ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_TOKEN"] = ah_token
        env["ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_VALIDATED_TOKEN"] = ah_token

    ansible_cfg = ctx.ee.ee_dir / "ansible.cfg"
    if ansible_cfg.exists():
        env["ANSIBLE_CONFIG"] = str(ansible_cfg)

    return env


def _is_transient(output: str) -> bool:
    return any(p in output for p in TRANSIENT_PATTERNS)


def _count_collections(output: str) -> int:
    count = output.count("was installed successfully")
    return count if count > 0 else output.count("Installing ")


def _parse_errors(output: str, findings: list[Finding]) -> None:
    if "Could not satisfy" in output or "Failed to resolve" in output:
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("*"):
                findings.append(Finding(
                    severity=Severity.ERROR,
                    message=f"Collection conflict: {line[2:]}",
                ))

    if "HTTP Error 400" in output or "Unauthorized" in output or "HTTP Error 401" in output:
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
        findings.append(Finding(
            severity=Severity.ERROR,
            message="Galaxy resolution failed (see verbose output for details)",
        ))
```

- [ ] **Step 2: Commit**

```bash
git add lib/ee_preflight/layers/galaxy.py
git commit -m "Add Layer 1: Galaxy resolution with transient error retry"
```

---

### Task 4: Layer 2 — Python/System Dep Validation

**Files:**
- Create: `lib/ee_preflight/layers/python_deps.py`

- [ ] **Step 1: Create Layer 2**

```python
# lib/ee_preflight/layers/python_deps.py
from __future__ import annotations
import subprocess
from pathlib import Path

from ..models import DepFormat, Finding, LayerResult, Severity, ValidateContext


def validate(ctx: ValidateContext) -> LayerResult:
    findings: list[Finding] = []

    _run_ade_check(ctx, findings)
    discovered_python, discovered_system = _run_introspect(ctx, findings)
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


def _run_introspect(
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
        line = line.strip()
        if line.startswith("python:"):
            section = "python"
            continue
        if line.startswith("system:"):
            section = "system"
            continue
        if not line or line.startswith("#"):
            continue

        if section == "python" and line.startswith("- "):
            dep = line[2:].strip().strip("'\"")
            dep = dep.split("#")[0].strip()
            if dep:
                python_deps.append(dep)
        elif section == "system" and line.startswith("- "):
            dep = line[2:].strip().strip("'\"")
            dep = dep.split("#")[0].strip()
            if dep:
                system_deps.append(dep)

    return python_deps, system_deps


def _diff_python_deps(
    ctx: ValidateContext,
    discovered: list[str],
    findings: list[Finding],
) -> None:
    declared = _read_declared_python(ctx)
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
    declared = _read_declared_system(ctx)
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


def _read_declared_python(ctx: ValidateContext) -> list[str]:
    if ctx.ee.python is None:
        return []
    if ctx.ee.python.format == DepFormat.FILE and ctx.ee.python.file_path:
        if ctx.ee.python.file_path.exists():
            return [
                l.strip()
                for l in ctx.ee.python.file_path.read_text().splitlines()
                if l.strip() and not l.strip().startswith("#")
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
                l.strip()
                for l in ctx.ee.system.file_path.read_text().splitlines()
                if l.strip() and not l.strip().startswith("#")
            ]
    if ctx.ee.system.format == DepFormat.INLINE:
        return [str(e) for e in ctx.ee.system.entries]
    return []


def _pkg_name(spec: str) -> str:
    for sep in [">=", "<=", "==", "!=", ">", "<", "[", ";"]:
        spec = spec.split(sep)[0]
    return spec.strip().lower().replace("-", "_")
```

- [ ] **Step 2: Commit**

```bash
git add lib/ee_preflight/layers/python_deps.py
git commit -m "Add Layer 2: Python/system dep discovery and validation"
```

---

### Task 5: Layer 3 — Container Wheel Build Test

**Files:**
- Create: `lib/ee_preflight/container.py`
- Create: `lib/ee_preflight/layers/system_deps.py`

- [ ] **Step 1: Create container runtime abstraction**

```python
# lib/ee_preflight/container.py
from __future__ import annotations
import shutil
import subprocess


class ContainerRuntime:
    def __init__(self) -> None:
        self.cmd = self._detect()

    def _detect(self) -> str:
        for cmd in ["podman", "docker"]:
            if shutil.which(cmd):
                return cmd
        raise RuntimeError(
            "No container runtime found. Install podman or docker for --container-test"
        )

    def pull(self, image: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.cmd, "pull", image],
            capture_output=True,
            text=True,
            timeout=300,
        )

    def run(self, image: str, command: str, timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.cmd, "run", "--rm", image, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @property
    def available(self) -> bool:
        try:
            self._detect()
            return True
        except RuntimeError:
            return False
```

- [ ] **Step 2: Create Layer 3**

```python
# lib/ee_preflight/layers/system_deps.py
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
    # Reuse introspect results from layer 2 if available via the venv
    from .python_deps import _find_site_packages, _run_introspect
    findings_ignored: list[Finding] = []
    python_deps, _ = _run_introspect(ctx, findings_ignored)
    return python_deps


def _find_source_only_packages(python_deps: list[str]) -> list[str]:
    known_source_only = {
        "systemd-python", "gssapi", "ncclient", "lxml",
        "ovirt-engine-sdk-python", "python-ldap", "pynacl",
    }
    source_pkgs = []
    for dep in python_deps:
        name = dep.split(">=")[0].split("==")[0].split("<")[0].split("[")[0].strip().lower()
        if name.replace("-", "_") in {k.replace("-", "_") for k in known_source_only}:
            source_pkgs.append(dep)
    return source_pkgs


def _get_bindep_install_cmd(ctx: ValidateContext) -> str:
    from .python_deps import _read_declared_system
    declared = _read_declared_system(ctx)
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
                message=f"{pkg_name} failed to build: {missing_file} not found (could not determine RPM)",
                fix=f"Manually find the RPM providing {missing_file}",
                source=f"required by {pkg_name}",
            ))
    else:
        findings.append(Finding(
            severity=Severity.ERROR,
            message=f"{pkg_name} failed to build (see output for details)",
            source=f"pip wheel output: {output[-200:]}",
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
    # Special case: Python.h → python3.X-devel
    if missing_file == "Python.h":
        return f"python{python_version}-devel"

    result = runtime.run(image, f"dnf provides '*/{missing_file}' 2>/dev/null || yum provides '*/{missing_file}' 2>/dev/null")
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("Last") and not line.startswith("=") and "-" in line:
                rpm_name = re.split(r"[-:]\d", line)[0]
                if rpm_name:
                    return rpm_name
    return None
```

- [ ] **Step 3: Commit**

```bash
git add lib/ee_preflight/container.py lib/ee_preflight/layers/system_deps.py
git commit -m "Add Layer 3: container wheel build test with dnf provides lookup"
```

---

### Task 6: Fixer

**Files:**
- Create: `lib/ee_preflight/fixer.py`

- [ ] **Step 1: Create fixer**

```python
# lib/ee_preflight/fixer.py
from __future__ import annotations
from pathlib import Path

import yaml

from .models import DepFormat, EEDefinition, Finding, Severity


def apply_fixes(ee: EEDefinition, findings: list[Finding]) -> list[str]:
    changes: list[str] = []

    system_fixes = [f for f in findings if f.fix and "bindep" in f.fix.lower()]
    python_fixes = [f for f in findings if f.fix and "python requirements" in f.fix.lower()]

    if system_fixes:
        entries = _extract_entries(system_fixes)
        _add_system_deps(ee, entries, changes)

    if python_fixes:
        entries = _extract_python_entries(python_fixes)
        _add_python_deps(ee, entries, changes)

    return changes


def _extract_entries(fixes: list[Finding]) -> list[str]:
    entries = []
    for f in fixes:
        if f.fix and "'" in f.fix:
            start = f.fix.index("'") + 1
            end = f.fix.index("'", start)
            entries.append(f.fix[start:end])
    return entries


def _extract_python_entries(fixes: list[Finding]) -> list[str]:
    entries = []
    for f in fixes:
        if f.fix and "'" in f.fix:
            start = f.fix.index("'") + 1
            end = f.fix.index("'", start)
            entries.append(f.fix[start:end])
    return entries


def _add_system_deps(
    ee: EEDefinition, entries: list[str], changes: list[str]
) -> None:
    if not entries:
        return

    if ee.system and ee.system.format == DepFormat.FILE and ee.system.file_path:
        existing = ee.system.file_path.read_text() if ee.system.file_path.exists() else ""
        existing_names = {line.split()[0] for line in existing.splitlines() if line.strip()}
        new_entries = [e for e in entries if e.split()[0] not in existing_names]
        if new_entries:
            with open(ee.system.file_path, "a") as f:
                for entry in new_entries:
                    f.write(f"{entry}\n")
            changes.append(f"Added to {ee.system.file_path.name}: {', '.join(new_entries)}")

    elif ee.system and ee.system.format == DepFormat.INLINE:
        _add_inline_deps(ee, "system", [e.split()[0] for e in entries], changes)

    else:
        # No system deps declared — create bindep.txt
        bindep_path = ee.ee_dir / "bindep.txt"
        with open(bindep_path, "w") as f:
            for entry in entries:
                f.write(f"{entry}\n")
        changes.append(f"Created {bindep_path.name} with: {', '.join(entries)}")


def _add_python_deps(
    ee: EEDefinition, entries: list[str], changes: list[str]
) -> None:
    if not entries:
        return

    if ee.python and ee.python.format == DepFormat.FILE and ee.python.file_path:
        existing = ee.python.file_path.read_text() if ee.python.file_path.exists() else ""
        existing_names = {line.split("=")[0].split(">")[0].split("<")[0].strip().lower()
                         for line in existing.splitlines() if line.strip() and not line.startswith("#")}
        new_entries = [e for e in entries if e.split("=")[0].split(">")[0].split("<")[0].strip().lower() not in existing_names]
        if new_entries:
            with open(ee.python.file_path, "a") as f:
                for entry in new_entries:
                    f.write(f"{entry}\n")
            changes.append(f"Added to {ee.python.file_path.name}: {', '.join(new_entries)}")

    elif ee.python and ee.python.format == DepFormat.INLINE:
        _add_inline_deps(ee, "python", entries, changes)


def _add_inline_deps(
    ee: EEDefinition, dep_type: str, entries: list[str], changes: list[str]
) -> None:
    with open(ee.path) as f:
        raw = yaml.safe_load(f)

    deps = raw.setdefault("dependencies", {})
    existing = deps.get(dep_type, [])
    if isinstance(existing, list):
        existing.extend(entries)
        deps[dep_type] = existing
    else:
        return

    with open(ee.path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

    changes.append(f"Added to {ee.path.name} [{dep_type}]: {', '.join(entries)}")
```

- [ ] **Step 2: Commit**

```bash
git add lib/ee_preflight/fixer.py
git commit -m "Add fixer: --fix writes missing deps back to EE files"
```

---

### Task 7: Runner and CLI

**Files:**
- Create: `lib/ee_preflight/runner.py`
- Create: `lib/ee_preflight/cli.py`
- Create: `bin/ee-preflight`

- [ ] **Step 1: Create runner**

```python
# lib/ee_preflight/runner.py
from __future__ import annotations
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from .ee_parser import parse_ee
from .fixer import apply_fixes
from .layers import prechecks, galaxy, python_deps, system_deps
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

    venv_path.mkdir(parents=True, exist_ok=True)

    ctx = ValidateContext(
        ee=ee,
        venv_path=venv_path,
        fix=fix,
        container_test=container_test,
        verbose=verbose,
    )

    results: list[LayerResult] = []

    try:
        # Layer 0
        r0 = prechecks.validate(ctx)
        results.append(r0)
        missing_files = any(
            f.severity == Severity.ERROR and "not found" in f.message
            for f in r0.findings
        )

        if missing_files:
            results.append(LayerResult(name="galaxy", status="skipped"))
            results.append(LayerResult(name="python_deps", status="skipped"))
            results.append(LayerResult(
                name="system_deps",
                status="skipped" if not container_test else "skipped",
            ))
            return results

        # Layer 1
        r1 = galaxy.validate(ctx)
        results.append(r1)

        if r1.has_errors:
            results.append(LayerResult(name="python_deps", status="skipped"))
            results.append(LayerResult(
                name="system_deps",
                status="skipped" if not container_test else "skipped",
            ))
        else:
            # Layer 2
            r2 = python_deps.validate(ctx)
            results.append(r2)

            # Layer 3
            r3 = system_deps.validate(ctx)
            results.append(r3)

        # --fix
        if fix:
            all_findings = [f for r in results for f in r.findings]
            fixable = [f for f in all_findings if f.fix and f.severity == Severity.WARNING]
            if fixable:
                changes = apply_fixes(ee, fixable)
                for change in changes:
                    results.append(LayerResult(
                        name="fix",
                        status="pass",
                        findings=[Finding(
                            severity=Severity.INFO,
                            message=change,
                        )],
                    ))

        # --build
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
    import os
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

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

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
```

- [ ] **Step 2: Create CLI**

```python
# lib/ee_preflight/cli.py
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


def _output_json(ee_path: Path, results: list[LayerResult]) -> None:
    overall = "fail" if any(r.has_errors for r in results) else "pass"
    output = {
        "ee": str(ee_path),
        "result": overall,
        "layers": [r.to_dict() for r in results],
    }
    print(json.dumps(output, indent=2))


def _output_human(ee_path: Path, results: list[LayerResult], verbose: bool) -> None:
    print(f"\nee-preflight: {ee_path}\n")

    layer_names = {
        "prechecks": "Layer 0: Pre-checks",
        "galaxy": "Layer 1: Galaxy Resolution",
        "python_deps": "Layer 2: Dependency Validation",
        "system_deps": "Layer 3: Container Wheel Test",
        "fix": "Fix",
        "build": "Build",
    }

    errors = 0
    warnings = 0

    for result in results:
        label = layer_names.get(result.name, result.name)

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
```

- [ ] **Step 3: Create entry point**

```python
#!/usr/bin/env python3
# bin/ee-preflight
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from ee_preflight.cli import main

main()
```

Make executable:
```bash
chmod +x bin/ee-preflight
```

- [ ] **Step 4: Commit**

```bash
git add lib/ee_preflight/runner.py lib/ee_preflight/cli.py bin/ee-preflight
git commit -m "Add runner, CLI, and entry point — ee-preflight is runnable"
```

---

### Task 8: Test against netbox-summit-2026-ee

- [ ] **Step 1: Run basic preflight**

```bash
bin/ee-preflight netbox-summit-2026-ee/execution-environment.yml --verbose
```

Expected: Layer 0 passes (or warns about ansible-lint formatting), Layer 1 resolves collections, Layer 2 reports findings, Layer 3 skipped.

- [ ] **Step 2: Run with --container-test**

```bash
bin/ee-preflight netbox-summit-2026-ee/execution-environment.yml --container-test --verbose
```

Expected: All layers run including container wheel test.

- [ ] **Step 3: Run with --json**

```bash
bin/ee-preflight netbox-summit-2026-ee/execution-environment.yml --json
```

Expected: Structured JSON output.

- [ ] **Step 4: Fix any issues found during testing, commit**

```bash
git add -A
git commit -m "Fix issues found during integration testing"
```

---

### Task 9: Final commit and push

- [ ] **Step 1: Push branch**

```bash
git push -u origin ee-preflight
```
