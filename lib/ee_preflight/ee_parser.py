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
