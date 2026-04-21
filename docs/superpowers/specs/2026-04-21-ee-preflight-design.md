# ee-preflight: Pre-build Validation for Ansible Execution Environments

## Problem

Building Ansible Execution Environments with `ansible-builder` is a slow trial-and-error loop. Each failure — collection version conflicts, missing Python deps, missing system `-devel` packages — requires a full container rebuild cycle (5-10+ minutes) to discover. Failures surface one at a time, so multiple issues mean multiple rebuild cycles.

In a real session building `netbox-summit-2026-ee`, we hit 5 sequential failures before a successful build: a Galaxy version conflict, a missing build arg, an AH timeout (transient), missing `systemd-devel`, missing `krb5-devel`, and missing `python3.12-devel`. A pre-build validation tool would have caught most of these in seconds.

## Solution

A Python CLI tool that validates an `execution-environment.yml` definition before running `ansible-builder`. It runs three validation layers in sequence, reporting all findings per layer before proceeding. If a layer fails, subsequent layers that depend on it are skipped.

## CLI Interface

```
ee-preflight <path/to/execution-environment.yml> [options]

Options:
  --fix                Automatically add discovered missing deps to the EE definition files
  --build              Run ansible-builder build after all layers pass
  --tag TAG            Image tag for --build (default: <ee-name>:latest)
  --venv PATH          Use this venv path (kept after run for inspection)
  --keep-venv          Keep the default temp venv after run
  --container-test     Enable layer 3: test wheel builds inside the base image
  --json               Output structured JSON instead of human-readable text
  --verbose            Show passing checks, not just failures
```

### Combining flags

`--fix --build` is the full end-to-end workflow: validate, fix what can be auto-fixed, then build. If `--fix` applies changes, the layers re-run to verify the fixes before proceeding to `--build`. If errors remain after `--fix` (e.g., version conflicts that require user judgment), the build is skipped and errors are reported.

`--build` without `--fix` only builds if all layers pass as-is.

`--build` passes `--build-arg` for any `ARG` declarations detected in Layer 0 that have matching env vars (e.g., `ARG AH_TOKEN` → `--build-arg AH_TOKEN=$AH_TOKEN`).

## Architecture

```
bin/ee-preflight                  # CLI entry point
lib/ee_preflight/
    __init__.py
    cli.py                        # arg parsing, output formatting
    runner.py                     # orchestrates layers, manages venv lifecycle
    models.py                     # Finding, LayerResult, Severity, EEDefinition
    ee_parser.py                  # parse execution-environment.yml (both formats)
    fixer.py                      # --fix: write missing deps back to EE files
    container.py                  # ContainerRuntime abstraction (podman, future docker)
    layers/
        __init__.py
        prechecks.py              # Layer 0: YAML lint, file refs, ARG/env checks
        galaxy.py                 # Layer 1: collection resolution
        python_deps.py            # Layer 2: Python dep discovery + validation
        system_deps.py            # Layer 3: system dep + wheel build test
```

### EE Definition Parsing

`ansible-builder` v3 supports two ways to declare dependencies:

**Separate files** (common in this repo):
```yaml
dependencies:
  galaxy: requirements.yml
  python: requirements.txt
  system: bindep.txt
```

**Inline** (what `ansible-creator init execution_env` generates):
```yaml
dependencies:
  galaxy:
    collections:
      - name: ansible.posix
  python:
    - boto3
    - requests
  system:
    - openssh-clients
```

The parser must detect which format is used for each dependency type (they can be mixed — e.g., galaxy in a file, python inline). This affects both reading (for validation) and writing (for `--fix`). The parser normalizes both formats into the same internal representation.

### --fix Mode

When `--fix` is passed, the tool writes discovered missing dependencies back to the EE definition instead of just reporting them. It respects whichever format the EE uses:

- **Separate files:** appends missing entries to `bindep.txt`, `requirements.txt`, etc.
- **Inline:** adds entries to the appropriate section in `execution-environment.yml`.

`--fix` only adds missing deps. It does not resolve version conflicts (those require user judgment — e.g., downgrade one collection or remove another). Conflicts are still reported as errors with suggested fixes.

After writing changes, `--fix` prints a summary of what was added and to which file.

Each layer is a function with the signature:

```python
def validate(context: ValidateContext) -> LayerResult
```

`ValidateContext` holds: venv path, parsed EE definition (base image, dep file paths, build steps), and auth config. `LayerResult` has a status (pass/fail/skipped) and a list of `Finding` objects.

### Data Model

```python
class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

@dataclass
class Finding:
    severity: Severity
    message: str
    fix: str | None           # actionable suggestion
    source: str | None        # dependency chain that caused it

@dataclass
class LayerResult:
    name: str
    status: str               # "pass", "fail", "skipped"
    findings: list[Finding]
```

## Layer 0: Pre-checks

**Purpose:** Catch basic structural problems before attempting any installs. Fast, no network, no venv needed.

**Implementation:**

1. **YAML lint** (optional): If `ansible-lint` is available, run it against `execution-environment.yml`. Report formatting issues. If `ansible-lint` is not installed, skip with an info message — it's not a hard requirement.
2. **File reference validation**: For each dependency declared as a file path (`galaxy: requirements.yml`, `python: requirements.txt`, `system: bindep.txt`), check the file exists relative to the EE directory.
3. **Build arg / env var check**: Parse `additional_build_steps` for `ARG` declarations. For each `ARG`, check whether the corresponding env var is set in the current shell. Warn if not — this catches the "missing `--build-arg AH_TOKEN`" class of failures.
4. **Base image format**: Validate the base image string looks like a valid registry path. Warn if it uses a SHA digest pin (not wrong, but unusual).

**Findings reported:**
- YAML formatting issues (from `ansible-lint`)
- Missing dependency files referenced by `execution-environment.yml`
- `ARG` declarations without matching env vars (e.g., `ARG AH_TOKEN` but `$AH_TOKEN` is not set)
- Malformed base image references

**Gate:** If dependency files are missing, layers 1-3 are skipped (nothing to validate). YAML formatting issues and missing env vars are reported as warnings — they don't block subsequent layers.

## Layer 1: Galaxy Resolution

**Purpose:** Verify all collections can be resolved and installed without version conflicts.

**Implementation:**
1. Parse `execution-environment.yml` to extract the collections requirements file path.
2. Set up auth: if `AH_TOKEN` env var is set, configure `ANSIBLE_GALAXY_SERVER_*` env vars for the `ade` subprocess. Otherwise, rely on the user's existing `ansible.cfg` or env config.
3. Run `ade install -r <requirements-file> --venv <path>`.
4. Parse output for errors: version conflicts, unreachable servers, auth failures, missing collections.
5. Report ALL errors found, not just the first.

**Transient error handling:** If `ade install` fails with a transient HTTP error (504, 502, 429, connection timeout, connection refused), the layer retries up to 3 times with exponential backoff (5s, 15s, 45s). If all retries fail, the error is reported as a finding with the retry history.

**Findings reported:**
- Collection version conflicts (e.g., `community.general:12.6.0` conflicts with `fedora.linux_system_roles` requirement `<12.0.0`)
- Galaxy server auth failures
- Collections not found on any configured server
- Transient errors that persisted after 3 retries

**Gate:** Layer 2 is skipped if layer 1 fails (collections must be installed to introspect deps).

## Layer 2: Dependency Discovery + Validation

**Purpose:** Discover all transitive Python and system dependencies from installed collections, validate they resolve, and check them against the user's declared deps.

**Implementation:**
1. Run `ade check --venv <path>` to validate installed collections. Parse its output for:
   - `pip install --dry-run --report` results (Python dep conflicts)
   - `bindep -b` results (missing system packages)
2. Run `ansible-builder introspect <venv-site-packages>` to get the full discovered dependency lists (Python and system).
3. Diff discovered Python deps against the user's declared `requirements.txt` / `python-packages.txt`:
   - Warn on undeclared transitive Python deps (deps the collections need but the user didn't list).
4. Diff discovered system deps against the user's declared `bindep.txt`:
   - Warn on undeclared system packages.
   - Provide the exact `bindep.txt` entry to add.

**Findings reported:**
- pip resolution conflicts between Python packages
- Undeclared Python dependencies (discovered by introspection but missing from user's file)
- Undeclared system dependencies (discovered by introspection but missing from user's `bindep.txt`)
- Drift between declared and discovered deps

**Gate:** Layer 3 is independent of layer 2 pass/fail (it tests against the base image, not the venv). However, if layer 1 failed, layer 3 is also skipped.

## Layer 3: Container Wheel Build Test (opt-in)

**Purpose:** Verify that Python packages with C extensions can actually compile inside the target base image. This catches undeclared system deps that collections don't properly declare in their metadata — the failures that only surface during `ansible-builder build`.

**Enabled by:** `--container-test` flag. Skipped by default (requires podman and pulls the base image).

**Implementation:**
1. Parse the base image from `execution-environment.yml`.
2. `podman pull` the base image.
3. From layer 2's discovered Python deps, identify source-only packages (packages that don't have a pre-built wheel for the target platform). Check with `pip download --dry-run --no-binary :all:` or inspect PyPI metadata.
4. For each source-only package, run inside the base image container:
   ```
   podman run --rm <base-image> sh -c "
     microdnf install -y <declared-bindep-packages> &&
     pip wheel --no-binary :all: <package>
   "
   ```
5. If a wheel build fails:
   a. Parse the error output for the missing file or command (e.g., `krb5-config: command not found`, `Python.h: No such file or directory`, `libsystemd.pc not found`).
   b. Run `dnf provides '*/<missing-file>'` inside the same container to find the RPM that provides it.
   c. Report the specific package to add to `bindep.txt`.
6. If `dnf provides` doesn't find a match, report the raw error and missing file for manual investigation.

**Findings reported:**
- C extension build failures with the specific missing `-devel` package and the `bindep.txt` entry to add
- Dependency chain that led to the source-only package (e.g., `ansible.eda → aiokafka[gssapi] → gssapi`)

**Python version detection:** The script detects the Python version inside the base image (by running `python3 --version` or checking `$PYCMD`) and uses the correct versioned `-devel` package name (e.g., `python3.12-devel` instead of `python3-devel`).

## Authentication

Three modes, checked in order:

1. **`AH_TOKEN` env var** — if set, the script configures `ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_TOKEN` and related env vars for Galaxy commands. Matches this repo's CI convention.
2. **`ansible.cfg` in the EE directory** — if the EE directory has an `ansible.cfg`, it's used via `ANSIBLE_CONFIG` env var (after checking it doesn't contain real tokens).
3. **System default** — fall back to whatever `ansible-galaxy` picks up from `~/.ansible.cfg` or env.

## Venv Lifecycle

- **Default:** creates `tmp/ee-preflight-<hash>/` in the current directory. Cleaned up after the run unless `--keep-venv` is passed.
- **`--venv PATH`:** uses the specified path. Created if it doesn't exist. Always kept after the run (user explicitly chose the path).
- The `tmp/` directory must be in `.gitignore` (already is in this repo).

## Output

### Human-readable (default)

```
ee-preflight: netbox-summit-2026-ee/execution-environment.yml

Layer 1: Galaxy Resolution ✓
  21 collections resolved

Layer 2: Dependency Validation ✗
  ✗ community.general:12.6.0 conflicts with fedora.linux_system_roles requirement <12.0.0
    → Pin community.general<12.0.0 or remove fedora.linux_system_roles
  ⚠ Undeclared system dep: krb5-devel (from ansible.eda → aiokafka[gssapi] → gssapi)
    → Add 'krb5-devel [platform:rpm]' to bindep.txt
  ⚠ Undeclared system dep: systemd-devel (from ansible.eda → systemd-python)
    → Add 'systemd-devel [platform:rpm]' to bindep.txt

Layer 3: Container Wheel Test (skipped, use --container-test)

Result: FAIL (1 error, 2 warnings)
```

### JSON (`--json`)

```json
{
  "ee": "netbox-summit-2026-ee/execution-environment.yml",
  "result": "fail",
  "layers": [
    {
      "name": "galaxy",
      "status": "pass",
      "findings": []
    },
    {
      "name": "python_deps",
      "status": "fail",
      "findings": [
        {
          "severity": "error",
          "message": "community.general:12.6.0 conflicts with fedora.linux_system_roles requirement <12.0.0",
          "fix": "Pin community.general<12.0.0 or remove fedora.linux_system_roles",
          "source": null
        },
        {
          "severity": "warning",
          "message": "Undeclared system dep: krb5-devel",
          "fix": "Add 'krb5-devel [platform:rpm]' to bindep.txt",
          "source": "ansible.eda → aiokafka[gssapi] → gssapi"
        }
      ]
    },
    {
      "name": "system_deps",
      "status": "skipped",
      "findings": []
    }
  ]
}
```

## Dependencies

### Required

| Tool | Minimum version | Purpose |
|---|---|---|
| Python | 3.10+ | Runtime (dataclasses, type hints, `\|` union syntax) |
| `ansible-dev-environment` (`ade`) | 24.0.0+ | Collection installation, dependency checking, introspection |
| `ansible-builder` | 3.0.0+ | Dependency introspection via `ansible-builder introspect` |
| `pip` | 22.0+ | Dry-run dependency resolution (`--dry-run --report`) |

### Optional

| Tool | Minimum version | Purpose |
|---|---|---|
| `podman` | 4.0+ | Layer 3: container wheel build tests |
| `ansible-lint` | 24.0.0+ | Layer 0: YAML formatting validation |
| `ansible-creator` | 25.0.0+ | EE scaffolding (future `--init` mode) |

The script checks tool availability and versions at startup. If a required tool is missing or too old, it exits with a clear message listing what to install. If `podman` is missing and `--container-test` is requested, it exits with an error specific to that flag.

### Container runtime abstraction

Layer 3 interacts with the container runtime through a thin abstraction (`ContainerRuntime` interface) that wraps `pull`, `run`, and `exec` operations. The v1 implementation supports `podman` only. The interface allows adding `docker` support later without changing layer 3 logic.

## Future Enhancements

- **Auto-populated dependency cache:** After layer 3 discovers a mapping (e.g., `gssapi` → `krb5-devel`), write it to a local `.ee-preflight-cache.json`. Subsequent runs check the cache first for instant answers, falling back to `dnf provides` for unknowns. Cache entries include the base image and timestamp so they can be invalidated.
- **Docker support:** Add `docker` as an alternative container runtime for layer 3 via the `ContainerRuntime` interface. Auto-detect which is available, or allow `--runtime docker|podman`.
- **Devcontainer support:** Run inside Ansible devcontainers where podman/docker may be accessed via docker-in-docker or a mounted socket. Detect the devcontainer environment and adjust runtime paths accordingly.
- **CI integration:** GitHub Action that runs `ee-preflight` on PRs before `ansible-builder`, failing fast with actionable output.
- **Base image collection diffing:** For `ee-supported` base images, inspect what's already installed and warn about collections/deps in the requirements that duplicate what's in the base image (the delta-only strategy).
- **`--init` mode:** Scaffold a new EE project using `ansible-creator init execution_env`, then run preflight validation on it. Combines scaffolding with validation in a single workflow.
