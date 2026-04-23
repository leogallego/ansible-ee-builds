# ee-preflight: Pre-build Validation for Ansible Execution Environments

## Problem

Building Ansible Execution Environments with `ansible-builder` is a slow trial-and-error loop. Each failure — collection version conflicts, missing Python deps, missing system `-devel` packages — requires a full container rebuild cycle (5-10+ minutes) to discover. Failures surface one at a time, so multiple issues mean multiple rebuild cycles.

In a real session building `netbox-summit-2026-ee`, we hit 7 sequential failures before a successful build: a Galaxy version conflict, a missing build arg, an AH timeout (transient), missing `systemd-devel`, missing `krb5-devel`, and missing `python3.12-devel`. A pre-build validation tool catches 6 of 7 in a single pass.

## Solution

A Python CLI tool that validates an `execution-environment.yml` definition before running `ansible-builder`. It runs four validation layers (0-3) in sequence, reporting all findings per layer before proceeding. If a layer fails, subsequent layers that depend on it are skipped. Layer 3 (container wheel test) auto-triggers when Layer 1 detects Python build failures — no manual opt-in needed.

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

**Purpose:** Verify all collections can be resolved and installed, along with their Python dependencies.

**Implementation:**
1. Parse `execution-environment.yml` to extract the collections requirements file path (or write inline collections to a temp file).
2. Set up auth: if `AH_TOKEN` env var is set, configure `ANSIBLE_GALAXY_SERVER_*` env vars (including certified and validated hub URLs). Otherwise, rely on the user's existing env config.
3. Run `ade install -r <requirements-file> --venv <path> --im none`. `ade` creates the venv, installs collections, discovers transitive Python deps, and attempts to install them.
4. Parse output: separate collection-level errors (Layer 1 failures) from Python build failures (passed to Layer 2 as warnings, auto-triggers Layer 3).
5. Report ALL collection errors found, not just the first.
6. `ade` may return non-zero even when collections installed successfully (e.g., venv not activated). Check for "Installed collections include:" in output as success signal.

**Transient error handling:** If `ade install` fails with a transient HTTP error (504, 502, 429, connection timeout, connection refused), the layer retries up to 3 times with exponential backoff (5s, 15s, 45s). If all retries fail, the error is reported as a finding with the retry history.

**Findings reported:**
- Collection version conflicts (e.g., `community.general:12.6.0` conflicts with `fedora.linux_system_roles` requirement `<12.0.0`)
- Galaxy server auth failures
- Collections not found on any configured server
- Transient errors that persisted after 3 retries
- Python dep build failures (as warnings, forwarded to Layer 2/3 for resolution)

**Gate:** Layer 2 is skipped if layer 1 fails (collections must be installed to introspect deps).

## Layer 2: Dependency Discovery + Validation

**Purpose:** Discover all transitive Python and system dependencies from installed collections, validate them against the user's declared deps, and incorporate any Python build failures from Layer 1.

**Implementation:**
1. Read `ade`'s `discovered_requirements.txt` and `discovered_bindep.txt` from `<venv>/.ansible-dev-environment/`. These files are annotated with source collections (e.g., `pytz  # from collection ansible.controller`).
2. Diff discovered Python deps against the user's declared `requirements.txt` / `python-packages.txt`:
   - Transitive deps are INFO (shown with `--verbose`). `ansible-builder` handles these automatically.
3. Diff discovered system deps against the user's declared `bindep.txt`:
   - Filter by target platform (inferred from base image name: RHEL 8/9, CentOS, Fedora, Debian).
   - Deduplicate entries.
   - Warn on undeclared system packages relevant to the target platform.
4. Incorporate Python build failure findings from Layer 1 (forwarded from `ade install`). These are warnings that defer RPM resolution to Layer 3 (which runs inside the target container).

**Findings reported:**
- Transitive Python deps (INFO, not actionable — ansible-builder handles them)
- Undeclared system deps for the target platform
- Python build failures from Layer 1 (deferred to Layer 3 for package resolution)

**Note:** `ansible-builder introspect` is not used. `ade` already performs introspection and writes the results with source collection annotations.

**Gate:** Layer 3 auto-triggers when Layer 1 reports Python build failures. Layer 3 is also available via `--container-test` for manual opt-in. If Layer 1 failed, both Layer 2 and Layer 3 are skipped.

## Layer 3: Container Wheel Build Test

**Purpose:** Verify that Python packages with C extensions can actually compile inside the target base image. This catches undeclared system deps that collections don't properly declare in their metadata — the failures that only surface during `ansible-builder build`. All package resolution happens inside the container, giving correct results regardless of host platform.

**Triggered by:**
- **Automatically** when Layer 1 (ade install) reports Python dep build failures. Failed package names are passed from Layer 1 so they get tested even if not in the known source-only list.
- **Manually** via `--container-test` flag (tests known source-only packages even without Layer 1 failures).
- Skipped if no container runtime is available and no build failures occurred.

**Implementation:**
1. Parse the base image from `execution-environment.yml`.
2. Pull the base image via podman (or docker).
3. Detect the Python version inside the base image (`python3 --version`).
4. Build the test package list from: known source-only packages (systemd-python, gssapi, ncclient, lxml, etc.) + packages that failed during ade install + extras that pull in source-only deps (e.g., `aiokafka[gssapi]` → also test `gssapi`). Deduplicated.
5. For each package, run inside the base image container:
   ```
   microdnf install -y <declared-bindep-packages> &&
   microdnf install -y python3-pip python3-devel gcc &&
   python3.X -m ensurepip &&
   python3.X -m pip wheel --no-binary :all: '<package>' -w /tmp/wheels
   ```
6. If a wheel build fails:
   a. Parse the error output for the missing file or command (e.g., `krb5-config: command not found`, `Python.h: No such file or directory`, `libsystemd.pc not found`).
   b. For `Python.h`: resolve to `python{version}-devel` using the detected Python version (e.g., `python3.12-devel`).
   c. For other files: install `dnf` inside the container (minimal images only have `microdnf`), then run `dnf provides` to find the providing package. Prefer `-devel` packages in results.
   d. For Debian-based containers: use `apt-file search` instead.
7. If resolution fails, report the raw error and missing file for manual investigation.

**Findings reported:**
- C extension build failures with the specific package to add to `bindep.txt`
- Correct package names for the target platform (not the host — e.g., `krb5-devel` on RHEL, not `heimdal-devel` from Fedora)

**Cumulative retry:** After testing all packages, if any missing RPMs were discovered, failed packages are retried with those RPMs installed. This repeats (up to 3 rounds) until no new RPMs are found. This catches dependencies hidden behind earlier failures — e.g., `gssapi` needs `python3.12-devel` to compile past `Python.h`, then reveals `krb5-devel` on the retry. All discovered RPMs are reported as fix suggestions regardless of which pass found them.

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
| `ansible-dev-environment` (`ade`) | 24.0.0+ | Collection installation, dependency discovery, introspection |
| `ansible-builder` | 3.0.0+ | Only needed for `--build` flag (runs the actual EE build) |

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

- ~~**Cumulative fix install in Layer 3:**~~ Implemented. Retry loop installs discovered RPMs and retests failed packages (up to 3 rounds).
- **Auto-populated dependency cache:** After Layer 3 discovers a mapping (e.g., `gssapi` → `krb5-devel`), write it to a local `.ee-preflight-cache.json`. Subsequent runs check the cache first for instant answers, falling back to `dnf provides` for unknowns. Cache entries include the base image and timestamp so they can be invalidated.
- **Docker support:** Add `docker` as an alternative container runtime for Layer 3 via the `ContainerRuntime` interface. Auto-detect which is available, or allow `--runtime docker|podman`.
- **Devcontainer support:** Run inside Ansible devcontainers where podman/docker may be accessed via docker-in-docker or a mounted socket. Detect the devcontainer environment and adjust runtime paths accordingly.
- **CI integration:** GitHub Action that runs `ee-preflight` on PRs before `ansible-builder`, failing fast with actionable output.
- **Base image collection diffing:** For `ee-supported` base images, inspect what's already installed and warn about collections/deps in the requirements that duplicate what's in the base image (the delta-only strategy).
- **`--init` mode:** Scaffold a new EE project using `ansible-creator init execution_env`, then run preflight validation on it. Combines scaffolding with validation in a single workflow.
- **Heavy collection warnings:** Flag collections with complex system deps (e.g., `ansible.eda` with systemd-python + gssapi) and suggest whether they belong in a standard EE vs. a DE.
