from __future__ import annotations

import yaml

from .models import DepFormat, EEDefinition, Finding, Severity


def apply_fixes(ee: EEDefinition, findings: list[Finding]) -> list[str]:
    changes: list[str] = []

    system_fixes = [f for f in findings if f.fix and "bindep" in f.fix.lower()]
    python_fixes = [f for f in findings if f.fix and "python requirements" in f.fix.lower()]

    if system_fixes:
        entries = _extract_quoted_entries(system_fixes)
        _add_system_deps(ee, entries, changes)

    if python_fixes:
        entries = _extract_quoted_entries(python_fixes)
        _add_python_deps(ee, entries, changes)

    return changes


def _extract_quoted_entries(fixes: list[Finding]) -> list[str]:
    entries: list[str] = []
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
        existing_names = {
            _pkg_name(line)
            for line in existing.splitlines()
            if line.strip() and not line.startswith("#")
        }
        new_entries = [e for e in entries if _pkg_name(e) not in existing_names]
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


def _pkg_name(spec: str) -> str:
    for sep in (">=", "<=", "==", "!=", ">", "<", "[", ";"):
        spec = spec.split(sep)[0]
    return spec.strip().lower().replace("-", "_")
