# Spec: Extract ee-preflight to Standalone Repository

**Date:** 2026-04-23
**Status:** Draft
**Source branch:** `ee-preflight` on `leogallego/ansible-ee-builds`
**Target repo:** `leogallego/ee-preflight`

## 1. Repository Structure

### New repo: `leogallego/ee-preflight`

Use **src layout** (`src/ee_preflight/`). This is the recommended Python packaging layout because it prevents accidental imports of the local source tree during testing -- the installed package is always what gets tested, not the working directory. It also avoids conflicts with the project root containing non-package files.

```
ee-preflight/
    src/
        ee_preflight/
            __init__.py
            cli.py
            runner.py
            models.py
            ee_parser.py
            fixer.py
            container.py
            layers/
                __init__.py
                prechecks.py
                galaxy.py
                python_deps.py
                system_deps.py
    tests/
        unit/
            test_models.py
            test_ee_parser.py
            test_fixer.py
            conftest.py
        integration/
            test_layer0.py
            test_layer1.py
            test_layer2.py
            test_layer3.py
            fixtures/
                minimal-ee/
                    execution-environment.yml
                    requirements.yml
                inline-ee/
                    execution-environment.yml
    docs/
        design.md                  # copied from 2026-04-21-ee-preflight-design.md
        build-report.md            # copied from 2026-04-21-netbox-summit-ee-build-report.md
    .github/
        workflows/
            ci.yml
            release.yml
    pyproject.toml
    README.md
    CLAUDE.md
    LICENSE
    .gitignore
```

### Files to copy from `ansible-ee-builds`

| Source | Destination | Notes |
|--------|-------------|-------|
| `lib/ee_preflight/*.py` | `src/ee_preflight/*.py` | No code changes needed -- `from __future__ import annotations` handles all type hints |
| `lib/ee_preflight/layers/*.py` | `src/ee_preflight/layers/*.py` | Same |
| `bin/ee-preflight` | Delete | Replaced by `pyproject.toml` console script entry point |
| `docs/superpowers/specs/2026-04-21-ee-preflight-design.md` | `docs/design.md` | Reference doc |
| `docs/superpowers/specs/2026-04-21-netbox-summit-ee-build-report.md` | `docs/build-report.md` | Motivating case study |

### What stays in `ansible-ee-builds`

- All EE definition directories
- CI workflows (`push-ee-build.yml`, `pr-ee-build.yml`, etc.)
- `requirements.txt` (ansible-builder pin)
- `ansible.cfg`
- `.github/`, `.devcontainer/`, `.vscode/`
- This spec (as historical record of the extraction)

## 2. Python Packaging

### Minimum Python version: 3.11

Set `requires-python = ">=3.11"`. Python 3.11 is the minimum supported target — it's available on RHEL 9 via AppStream (`python3.11`), ships as default on Fedora 37+, and aligns with AAP 2.5+ base images which use Python 3.11+.

The code is technically 3.9-compatible (`from __future__ import annotations` makes type union syntax safe at runtime), but 3.11 gives us `tomllib` in stdlib, better error messages, and ExceptionGroup support for future use. RHEL 9 users who only have system Python 3.9 can install 3.11 via `dnf install python3.11`.

### `pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=68.0", "setuptools-scm>=8.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "ee-preflight"
version = "0.1.0"
description = "Pre-build validation for Ansible Execution Environments"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.11"
authors = [
    {name = "Leonardo Gallego"},
]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: Apache Software License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Software Development :: Build Tools",
    "Topic :: System :: Systems Administration",
]
dependencies = [
    "pyyaml>=6.0",
    "ansible-dev-environment>=24.0.0",
]

[project.optional-dependencies]
lint = [
    "ansible-lint>=24.0.0",
]
build = [
    "ansible-builder>=3.0.0,<3.1.0",
]
dev = [
    "pytest>=7.0",
    "pytest-cov>=4.0",
    "ruff>=0.4.0",
    "mypy>=1.10",
]

[project.scripts]
ee-preflight = "ee_preflight.cli:main"

[project.urls]
Homepage = "https://github.com/leogallego/ee-preflight"
Repository = "https://github.com/leogallego/ee-preflight"
Issues = "https://github.com/leogallego/ee-preflight/issues"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
target-version = "py311"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "integration: tests that require ade, podman, or network access",
]
```

### Actual imports (verified from source)

| Module | Imports | PyPI package |
|--------|---------|--------------|
| `ee_parser.py` | `import yaml` | `pyyaml` |
| `galaxy.py` | `import yaml` | `pyyaml` |
| `fixer.py` | `import yaml` | `pyyaml` |
| `galaxy.py` | subprocess call to `ade` | `ansible-dev-environment` (CLI tool) |
| `prechecks.py` | subprocess call to `ansible-lint` | optional, not a Python import |
| `runner.py` | subprocess call to `ansible-builder` | optional, only for `--build` |
| `container.py` | subprocess call to `podman`/`docker` | system tool, not a pip dep |

All other imports are stdlib: `argparse`, `dataclasses`, `enum`, `hashlib`, `json`, `os`, `pathlib`, `re`, `shutil`, `subprocess`, `sys`, `time`.

### Code change required for src layout

The `bin/ee-preflight` script with its `sys.path` hack is replaced entirely by the `[project.scripts]` entry point in `pyproject.toml`. No changes to the library code are needed -- the package name `ee_preflight` stays the same, and all internal imports use relative paths (e.g., `from .models import ...`, `from ..container import ...`).

## 3. CI/CD

### GitHub Actions: `ci.yml`

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install ruff
      - run: ruff check src/ tests/
      - run: ruff format --check src/ tests/

  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install mypy pyyaml types-PyYAML
      - run: mypy src/ee_preflight/

  test-unit:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: pytest tests/unit/ -v --cov=ee_preflight --cov-report=term-missing

  test-integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev,build]"
      - run: pytest tests/integration/ -v -m integration --timeout=300
```

### GitHub Actions: `release.yml`

```yaml
name: Release
on:
  push:
    tags: ["v*"]

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: release
    permissions:
      id-token: write  # for trusted publishing
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install build
      - run: python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

### Test strategy

**Unit tests** (no external tools needed):
- `test_models.py` -- `Finding.to_dict()`, `LayerResult.has_errors`, `EEDefinition.build_args`
- `test_ee_parser.py` -- parse v2 and v3 EE files, inline vs file-ref deps, base image extraction
- `test_fixer.py` -- `apply_fixes` with file-ref deps, inline deps, creating new files, deduplication
- `test_cli.py` -- arg parsing, output formatting (mock `run()`)

**Integration tests** (marked with `@pytest.mark.integration`):
- Layer 0: needs `ansible-lint` installed (optional -- test both paths)
- Layer 1: needs `ade` installed and network access to Galaxy
- Layer 2: needs Layer 1 to have run (reads ade output files)
- Layer 3: needs `podman` and network access to pull base images

Integration tests use fixture EE directories under `tests/integration/fixtures/` with minimal collection lists (e.g., `ansible.posix` only) to keep CI fast.

### Handling the `ade` dependency in CI

`ansible-dev-environment` is declared as a pip dependency, so `pip install -e ".[dev]"` installs both the Python library and the `ade` CLI. No special handling needed. For integration tests, `ade` needs network access to Galaxy -- this runs in the `test-integration` job only.

## 4. Documentation

### README.md

Structure:
1. One-line description: "Pre-build validation for Ansible Execution Environments"
2. What it does (catches dependency conflicts, missing system packages, auth issues before `ansible-builder build`)
3. Installation: `pip install ee-preflight`
4. Quick start: `ee-preflight path/to/execution-environment.yml`
5. CLI reference (all flags, with examples)
6. Layer descriptions (0-3, what each catches)
7. `--fix` workflow: `ee-preflight --fix --build path/to/ee.yml`
8. Requirements: Python 3.11+, ade (auto-installed), podman (optional, for Layer 3)
9. Link to design doc and build report

### CLAUDE.md for the new repo

```markdown
# CLAUDE.md

## Project Overview

ee-preflight is a Python CLI tool that validates Ansible Execution Environment
definitions before running ansible-builder. It runs 4 validation layers
(prechecks, galaxy resolution, dependency validation, container wheel test)
and reports all issues in a single pass.

## Build Commands

    pip install -e ".[dev]"          # install in dev mode
    pytest tests/unit/ -v            # run unit tests
    pytest tests/ -v -m integration  # run integration tests (needs ade + podman)
    ruff check src/ tests/           # lint
    mypy src/ee_preflight/           # type check

## Architecture

src/ee_preflight/
    cli.py         -- arg parsing, output formatting
    runner.py      -- orchestrates layers, manages venv lifecycle
    models.py      -- Finding, LayerResult, Severity, EEDefinition
    ee_parser.py   -- parse execution-environment.yml
    fixer.py       -- --fix: write missing deps back to EE files
    container.py   -- ContainerRuntime abstraction (podman/docker)
    layers/
        prechecks.py    -- Layer 0: YAML lint, file refs, build args
        galaxy.py       -- Layer 1: ade install for collection resolution
        python_deps.py  -- Layer 2: discovered dep diffing
        system_deps.py  -- Layer 3: container wheel build test

## Key Design Decisions

- Uses `ade install` (not `ansible-galaxy`) for Layer 1
- Uses ade's discovered_requirements.txt (not `ansible-builder introspect`) for Layer 2
- All RPM resolution happens inside the target container (Layer 3), not on the host
- `from __future__ import annotations` in all modules (3.9-compat, minimum target is 3.11)
- pyyaml is the only Python library dependency; ade is a CLI tool dependency

## Testing

Unit tests must not require ade, podman, or network. Use fixtures and mocks.
Integration tests are marked with @pytest.mark.integration.
```

### Design doc and build report

Copy as-is to `docs/design.md` and `docs/build-report.md`. Add a note at the top of each:

```
> Moved from leogallego/ansible-ee-builds (branch ee-preflight).
> Original path: docs/superpowers/specs/2026-04-21-ee-preflight-design.md
```

## 5. Migration Checklist

Execute in this order:

### Phase 1: Create the new repo

- [ ] Create `leogallego/ee-preflight` on GitHub (empty, with Apache-2.0 license)
- [ ] Clone it locally
- [ ] Create the `src/ee_preflight/` directory structure
- [ ] Copy all `.py` files from `lib/ee_preflight/` to `src/ee_preflight/` (preserving `layers/` subdirectory)
- [ ] Create `pyproject.toml` as specified in section 2
- [ ] Create `.gitignore` with: `__pycache__/`, `*.pyc`, `dist/`, `build/`, `*.egg-info/`, `.eggs/`, `tmp/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.venv/`, `*.egg`
- [ ] Create `LICENSE` (Apache-2.0)
- [ ] Copy docs to `docs/design.md` and `docs/build-report.md`
- [ ] Write `README.md`
- [ ] Write `CLAUDE.md`

### Phase 2: Verify the extracted code works

- [ ] Create a venv and install: `pip install -e ".[dev]"`
- [ ] Verify `ee-preflight --help` works
- [ ] Run against a test EE: `ee-preflight /path/to/some/execution-environment.yml --verbose`
- [ ] Run with `--container-test` if podman is available
- [ ] Run with `--json`

### Phase 3: Add tests

- [ ] Write unit tests for `models.py`, `ee_parser.py`, `fixer.py`
- [ ] Create test fixture EE directories under `tests/integration/fixtures/`
- [ ] Write integration test stubs (may skip in CI initially if ade/podman not available)
- [ ] Verify `pytest tests/unit/` passes

### Phase 4: Add CI

- [ ] Create `.github/workflows/ci.yml`
- [ ] Create `.github/workflows/release.yml`
- [ ] Push to GitHub, verify CI passes
- [ ] Fix any lint/type issues surfaced by ruff/mypy (the existing code has no type stubs for some return values)

### Phase 5: Update ansible-ee-builds

- [ ] On the `ee-preflight` branch, add a `MOVED.md` in `lib/ee_preflight/` pointing to the new repo
- [ ] Update the design spec to note the extraction
- [ ] Update memory files to reference the new repo
- [ ] Merge `ee-preflight` branch to `main` (or leave it as a historical branch -- decide based on whether the EE definitions on that branch should be merged)

### Phase 6: Initial release

- [ ] Tag `v0.1.0` on the new repo
- [ ] Verify release workflow publishes to PyPI (or test with TestPyPI first)

### Git history approach

Use a **fresh repo** (not `git subtree split`). Reasons:

1. The tool's history is interleaved with unrelated EE definition changes on the `ee-preflight` branch.
2. The directory structure changes (`lib/` to `src/`), making `subtree split` produce confusing history.
3. The tool has only ~15 commits. The design spec and build report capture the rationale and evolution better than commit messages.
4. The `ee-preflight` branch on `ansible-ee-builds` serves as the historical record.

Initial commit message: `"Extract ee-preflight from leogallego/ansible-ee-builds (branch ee-preflight)"` with a link to the source branch.

### Links between repos

- `ee-preflight` README links to `ansible-ee-builds` as the "EE definitions repository where this tool was developed"
- `ansible-ee-builds` `MOVED.md` (or README update) links to `ee-preflight` as "the preflight tool has moved to its own repo"
- GitHub issue on `ansible-ee-builds` documenting the extraction (cross-referenced from memory files)

## 6. Future Packaging

### PyPI publication

- Use PyPA trusted publishing (OIDC, no API tokens to manage)
- Publish on `v*` tags via `release.yml`
- Test with TestPyPI before the first real release: `python -m build && twine upload --repository testpypi dist/*`
- Reserve the `ee-preflight` name on PyPI early (publish 0.1.0 even if alpha quality)

### RPM / Fedora packaging

Considerations for eventual Fedora/EPEL packaging:

- `ansible-dev-environment` must also be packaged (check if it already is in Fedora repos)
- `pyyaml` is already available as `python3-pyyaml` in all target distros
- The `ee-preflight` console script maps cleanly to `/usr/bin/ee-preflight`
- System deps (`podman`, `ansible-lint`) are already packaged in Fedora/RHEL
- A `.spec` file can be generated from `pyproject.toml` using `pyp2spec`
- Target repos: Fedora (community), EPEL 9 (RHEL users), COPR (faster iteration before official packaging)

### Container image

A container image with ee-preflight pre-installed is useful for CI pipelines that don't want to install Python packages:

```dockerfile
FROM registry.fedoraproject.org/fedora-minimal:41
RUN microdnf install -y python3.12 python3.12-pip podman && \
    python3.12 -m pip install ee-preflight && \
    microdnf clean all
ENTRYPOINT ["ee-preflight"]
```

Considerations:
- The image needs podman-in-podman (or a socket mount) for Layer 3 to work
- Publish to `quay.io/leogallego/ee-preflight`
- Tag with the tool version: `quay.io/leogallego/ee-preflight:0.1.0`
- Add a `Containerfile` to the repo root
- Build and push via a separate CI workflow on release tags

### GitHub Action

A custom GitHub Action wrapping ee-preflight would let EE repos add preflight checks to their PR workflows:

```yaml
- uses: leogallego/ee-preflight-action@v1
  with:
    ee-path: my-ee/execution-environment.yml
    container-test: true
```

This is a stretch goal after the tool stabilizes. The action would install ee-preflight from PyPI and run it, or use the container image.
