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
    status: str
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
    file_path: Path | None = None
    entries: list = field(default_factory=list)


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
        args: list[str] = []
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
