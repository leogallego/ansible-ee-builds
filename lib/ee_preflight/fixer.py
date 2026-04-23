from __future__ import annotations

import re
import yaml

from .models import DepFormat, EEDefinition, Finding, Severity


def apply_fixes(ee: EEDefinition, findings: list[Finding]) -> list[str]:
    changes: list[str] = []

    system_fixes = [f for f in findings if f.fix and "bindep" in f.fix.lower()]

    if system_fixes:
        entries = _extract_quoted_entries(system_fixes)
        _add_system_deps(ee, entries, changes)

    # NOTE: No layer currently produces fix text matching Python requirements.
    # _add_python_deps() is kept as a utility for future use when a layer
    # emits Python-specific fix suggestions.

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
        _add_inline_deps(ee, "system", entries, changes)

    else:
        bindep_path = ee.ee_dir / "bindep.txt"
        with open(bindep_path, "w") as f:
            for entry in entries:
                f.write(f"{entry}\n")
        changes.append(f"Created {bindep_path.name} with: {', '.join(entries)}")
        _add_dep_ref_to_ee(ee, "system", "bindep.txt", changes)


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

    else:
        reqs_path = ee.ee_dir / "requirements.txt"
        with open(reqs_path, "w") as f:
            for entry in entries:
                f.write(f"{entry}\n")
        changes.append(f"Created {reqs_path.name} with: {', '.join(entries)}")
        _add_dep_ref_to_ee(ee, "python", "requirements.txt", changes)


def _add_dep_ref_to_ee(
    ee: EEDefinition, dep_type: str, filename: str, changes: list[str]
) -> None:
    lines = ee.path.read_text().splitlines(keepends=True)

    # Check if the dep_type already exists under dependencies
    with open(ee.path) as f:
        raw = yaml.safe_load(f)
    deps = raw.get("dependencies", {})
    if deps and dep_type in deps:
        return

    # Find the dependencies: line and determine insertion point
    dep_line_idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("dependencies:"):
            dep_line_idx = i
            break

    if dep_line_idx is None:
        # No dependencies key — append one at end of file
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("dependencies:\n")
        dep_line_idx = len(lines) - 1

    # Determine the indentation used by existing entries under dependencies
    base_indent = len(lines[dep_line_idx]) - len(lines[dep_line_idx].lstrip())
    child_indent = base_indent + 2  # default child indent

    # Scan entries under dependencies: to find the last one
    last_dep_entry_idx = dep_line_idx
    for i in range(dep_line_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            last_dep_entry_idx = i
            continue
        current_indent = len(lines[i]) - len(lines[i].lstrip())
        if current_indent > base_indent:
            child_indent = current_indent
            last_dep_entry_idx = i
        else:
            break

    # Insert the new dependency reference after the last entry
    new_line = f"{' ' * child_indent}{dep_type}: {filename}\n"
    lines.insert(last_dep_entry_idx + 1, new_line)

    ee.path.write_text("".join(lines))
    changes.append(f"Added '{dep_type}: {filename}' to {ee.path.name}")


def _add_inline_deps(
    ee: EEDefinition, dep_type: str, entries: list[str], changes: list[str]
) -> None:
    # Verify the dep_type is inline (list or dict with a nested list)
    with open(ee.path) as f:
        raw = yaml.safe_load(f)
    deps = raw.get("dependencies", {})
    existing = deps.get(dep_type)

    # Galaxy deps can be a dict with a "collections" key containing a list;
    # python/system deps are plain lists.
    if isinstance(existing, dict):
        existing_list = existing.get("collections", [])
        is_galaxy_dict = True
    elif isinstance(existing, list):
        existing_list = existing
        is_galaxy_dict = False
    else:
        return

    # Filter out entries already present
    existing_set = set(str(e) for e in existing_list)
    new_entries = [e for e in entries if e not in existing_set]
    if not new_entries:
        return

    lines = ee.path.read_text().splitlines(keepends=True)

    # Find the dep_type key under dependencies
    dep_line_idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("dependencies:"):
            dep_line_idx = i
            break

    if dep_line_idx is None:
        return

    # Find the dep_type: line under dependencies
    type_line_idx = None
    base_indent = len(lines[dep_line_idx]) - len(lines[dep_line_idx].lstrip())
    for i in range(dep_line_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        current_indent = len(lines[i]) - len(lines[i].lstrip())
        if current_indent <= base_indent:
            break  # left the dependencies block
        if re.match(rf"^\s+{re.escape(dep_type)}\s*:", lines[i]):
            type_line_idx = i
            break

    if type_line_idx is None:
        return

    # For galaxy dict format, find the "collections:" subkey line
    search_start = type_line_idx
    if is_galaxy_dict:
        type_indent = len(lines[type_line_idx]) - len(lines[type_line_idx].lstrip())
        for i in range(type_line_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#"):
                continue
            current_indent = len(lines[i]) - len(lines[i].lstrip())
            if current_indent <= type_indent:
                break
            if re.match(r"^\s+collections\s*:", lines[i]):
                search_start = i
                break

    # Find the last list item (- entry) under this dep_type (or collections subkey)
    search_indent = len(lines[search_start]) - len(lines[search_start].lstrip())
    item_indent = search_indent + 2  # default
    last_item_idx = search_start
    for i in range(search_start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            last_item_idx = i
            continue
        current_indent = len(lines[i]) - len(lines[i].lstrip())
        if current_indent <= search_indent:
            break  # left the block
        if stripped.startswith("- "):
            item_indent = current_indent
            last_item_idx = i

    # Insert new entries after the last list item
    insert_lines = [f"{' ' * item_indent}- {entry}\n" for entry in new_entries]
    for offset, new_line in enumerate(insert_lines):
        lines.insert(last_item_idx + 1 + offset, new_line)

    ee.path.write_text("".join(lines))
    changes.append(f"Added to {ee.path.name} [{dep_type}]: {', '.join(new_entries)}")


def _pkg_name(spec: str) -> str:
    for sep in (">=", "<=", "==", "!=", ">", "<", "[", ";"):
        spec = spec.split(sep)[0]
    return spec.strip().lower().replace("-", "_")
