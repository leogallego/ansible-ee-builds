# Build Report: netbox-summit-2026-ee

**Date:** 2026-04-21
**Base image:** aap-26 / ee-minimal-rhel9
**Builder version:** ansible-builder 3.0.0
**Total build attempts:** 8 (7 failures, 1 success)
**Estimated total time:** ~75 minutes (including research, fixes, and rebuilds)

## Summary

| # | Action | Result | Root Cause | Fix Applied | Est. Time |
|---|--------|--------|-----------|-------------|-----------|
| 0 | Research & scaffold | — | — | Looked up 21 collection versions (Galaxy API + Automation Hub API), created EE files | ~15 min |
| 1 | Build #1 | FAIL | `AH_TOKEN` not passed as `--build-arg` | Added `--build-arg AH_TOKEN=$AH_TOKEN` to build command | ~3 min |
| 2 | Build #2 | FAIL | `community.general:12.6.0` conflicts with `fedora.linux_system_roles` requiring `<12.0.0` | Removed `fedora.linux_system_roles` from collections | ~5 min |
| 3 | Build #3 | FAIL | Automation Hub 504 Gateway Timeout | Retried (transient) | ~5 min |
| 4 | Build #4 | FAIL | `systemd-python` can't find `libsystemd.pc` | Added `systemd-devel [platform:rpm]` to `bindep.txt` | ~8 min |
| 5 | Build #5 | FAIL | `gssapi` can't find `krb5-config` | Added `krb5-devel [platform:rpm]` to `bindep.txt` | ~8 min |
| 6 | Build #6 | FAIL | `gssapi` + `systemd-python` can't find `Python.h` | Added `python3-devel [platform:rpm]` to `bindep.txt` | ~8 min |
| 7 | Build #7 | FAIL | Still can't find `Python.h` — `python3-devel` installs 3.9 headers, but build uses Python 3.12 | Changed `python3-devel` to `python3.12-devel` in `bindep.txt` | ~8 min |
| 8 | Build #8 | **PASS** | — | — | ~10 min |

**Total build time (builds only):** ~55 min
**Total session time (including research, version lookups, fixes):** ~75 min

## What would ee-preflight have caught?

| Failure | Layer | Would it catch it? |
|---------|-------|--------------------|
| Missing `--build-arg` | — | No (build command issue, not an EE definition issue) |
| `community.general` vs `fedora.linux_system_roles` conflict | Layer 1 (Galaxy) | **Yes** — `ade install` would report the version conflict |
| AH 504 timeout | — | No (transient network issue) |
| Missing `systemd-devel` | Layer 2/3 | **Yes** — introspection discovers `systemd-python`, layer 3 catches the missing `-devel` |
| Missing `krb5-devel` | Layer 2/3 | **Yes** — introspection discovers `gssapi` via `ansible.eda → aiokafka[gssapi]`, layer 3 catches it |
| Missing `python3-devel` | Layer 3 | **Yes** — wheel build test inside the base image would fail the same way |
| Wrong `python3-devel` version | Layer 3 | **Yes** — running inside the actual base image would detect the 3.9 vs 3.12 mismatch and `dnf provides` would suggest `python3.12-devel` |

**5 of 7 failures would have been caught in a single preflight run, before the first `ansible-builder build`.**

## Detailed Timeline

### Phase 1: Research and Scaffolding (~15 min)

Starting point: `netbox-for-nuno` EE as the base template.

**Collection version lookups:**
- 17 collections resolved via public Galaxy API (`curl` against `galaxy.ansible.com`)
- 4 collections required Automation Hub API (`console.redhat.com`): `ansible.controller`, `ansible.platform`, `ansible.eda`, `ansible.hub`
- AH API requires exchanging the offline `AH_TOKEN` for an access token via Red Hat SSO before querying

**Collections added vs. base (`netbox-for-nuno`):**
- Added: `ansible.security`, `cisco.dnac`, `ansible.eda`, `ansible.hub`, `ansible.platform`
- Removed (later): `fedora.linux_system_roles`, `redhat.rhel_idm`
- Deduplicated: `cisco.ios` and `arista.eos` were listed twice in the original

**Python packages pinned:**
- `jmespath==1.1.0`, `pytz==2026.1.post1`, `pynetbox==7.6.1`, `cryptography==45.0.7`
- `cryptography` pinned to `<46` range due to known Rust build requirement changes in 46.x

**Files created:**
- `execution-environment.yml` — v3, aap-26/ee-minimal-rhel9, PYCMD pinned to python3.12
- `ansible-collections.yml` — 21 collections, all pinned
- `python-packages.txt` — 4 packages, all pinned
- `bindep.txt` — initial: `python3-systemd`, `pkgconf-pkg-config`
- `ansible.cfg` — copied from `netbox-for-nuno`

### Phase 2: Build Attempts

#### Build #1 — Missing build arg (~3 min)

```
ansible-builder build -f netbox-summit-2026-ee/execution-environment.yml -t netbox-summit-2026-ee:test -v 3
```

**Failed at:** Galaxy stage (stage 2)
**Error:** `HTTP Error 400: Bad Request` from Automation Hub — the `AH_TOKEN` build arg wasn't passed, so the `ARG AH_TOKEN` / `ENV ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_TOKEN=$AH_TOKEN` in `prepend_galaxy` resolved to empty.
**Root cause:** Build command missing `--build-arg AH_TOKEN=$AH_TOKEN`.
**Fix:** Added `--build-arg AH_TOKEN=$AH_TOKEN` to the build command. Also had to restore `ansible.cfg` — it had been accidentally overwritten with the real token via `sed` from an earlier attempt.

#### Build #2 — Collection version conflict (~5 min)

```
ansible-builder build ... --build-arg AH_TOKEN=$AH_TOKEN
```

**Failed at:** Galaxy stage (stage 2), collection install
**Error:**
```
Could not satisfy the following requirements:
* community.general:12.6.0 (direct request)
* community.general:>=6.6.0,<12.0.0 (dependency of fedora.linux_system_roles:1.121.0)
```
**Root cause:** `fedora.linux_system_roles 1.121.0` requires `community.general <12.0.0`, but we pinned `community.general 12.6.0`.
**Fix:** Removed `fedora.linux_system_roles` from `ansible-collections.yml` (user decision to keep the newer `community.general`).

#### Build #3 — Automation Hub 504 (~5 min)

**Failed at:** Galaxy stage (stage 2), downloading `ansible-controller`
**Error:** `HTTP Error 504: Gateway Time-out` from Automation Hub
**Root cause:** Transient Automation Hub outage.
**Fix:** Retried the same build command.

#### Build #4 — Missing systemd-devel (~8 min)

**Failed at:** Builder stage (stage 3), pip wheel compilation
**Error:**
```
Cannot find libsystemd or libsystemd-journal:
Package libsystemd was not found in the pkg-config search path.
```
**Root cause:** `systemd-python` (pulled in by `ansible.eda`) needs `libsystemd.pc` to build, which comes from the `systemd-devel` RPM. Not in `bindep.txt`.
**Fix:** Added `systemd-devel [platform:rpm]` to `bindep.txt`.

#### Build #5 — Missing krb5-devel (~8 min)

**Failed at:** Builder stage (stage 3), pip wheel compilation
**Error:**
```
/bin/sh: line 1: krb5-config: command not found
subprocess.CalledProcessError: Command 'krb5-config --libs gssapi' returned non-zero exit status 127.
ERROR: Failed to build 'gssapi' when getting requirements to build wheel
```
**Root cause:** `ansible.eda` → `aiokafka[gssapi]` → `gssapi` Python package needs `krb5-config` binary, which comes from `krb5-devel` RPM.
**Fix:** Added `krb5-devel [platform:rpm]` to `bindep.txt`.

#### Build #6 — Missing Python.h (wrong package) (~8 min)

**Failed at:** Builder stage (stage 3), pip wheel compilation
**Error:**
```
gssapi/raw/misc.c:52:10: fatal error: Python.h: No such file or directory
```
**Root cause:** Both `gssapi` and `systemd-python` need Python development headers (`Python.h`) to compile their C extensions.
**Fix:** Added `python3-devel [platform:rpm]` to `bindep.txt`. **This turned out to be wrong.**

#### Build #7 — Wrong python3-devel version (~8 min)

**Failed at:** Builder stage (stage 3), same `Python.h` error
**Error:** Identical to build #6 — `Python.h: No such file or directory`
**Root cause:** `python3-devel` installs headers for Python 3.9 (the system `python3`), but the build uses Python 3.12 (set via `ENV PYCMD=/usr/bin/python3.12`). The compiler looks in `/usr/include/python3.12/` which doesn't exist. The base image ships Python 3.12 as the primary runtime but the system `python3` symlink still points to 3.9.
**Fix:** Changed `python3-devel` to `python3.12-devel` in `bindep.txt`.

#### Build #8 — Success (~10 min)

**All four stages completed.**

**Verification:**
```
$ podman run --rm netbox-summit-2026-ee:test ansible --version
ansible [core 2.16.17]

$ podman run --rm netbox-summit-2026-ee:test ansible-galaxy collection list
# 21 pinned collections + 4 transitive deps installed
```

### Phase 3: Post-build

- Committed and pushed to feature branch
- Opened PR #95 against upstream `ansible-tmm/ee-builds`
- Merged via squash merge
- Synced fork, cleaned up branches
- Documented pinned versions and build notes in GitHub issue #3

## Final bindep.txt

```
python3-systemd
systemd-devel [platform:rpm]
krb5-devel [platform:rpm]
python3.12-devel [platform:rpm]
pkgconf-pkg-config
```

## Final ansible-collections.yml

21 collections pinned. Removed from initial plan:
- `fedora.linux_system_roles` — version conflict with `community.general 12.x`
- `redhat.rhel_idm` — pulls `gssapi` Python dep chain requiring `krb5-devel` + `python3.12-devel`; removed to reduce system deps (can be re-added with the bindep entries above)

## Key Lessons

1. **`ansible-builder` failures are sequential** — each build cycle reveals at most one new issue. A single preflight validation would have surfaced the Galaxy conflict + all three missing system deps simultaneously.

2. **Collection metadata doesn't declare transitive system deps** — `ansible.eda` depends on `aiokafka[gssapi]` which needs `krb5-devel`, but this isn't in any `bindep.txt` in the collection. The only way to discover it is to attempt the pip install.

3. **Python version mismatch is a trap** — `python3-devel` ≠ `python3.12-devel` on RHEL 9 when the base image uses a non-default Python version. This is specific to `ee-minimal-rhel9` where `python3` is 3.9 but the EE runtime is 3.12.

4. **Automation Hub auth has two failure modes** — missing `--build-arg` (silent empty token) and transient 504s. Both waste a full build cycle.

5. **Version lookups require two APIs** — public Galaxy for community/validated collections, Automation Hub (with SSO token exchange) for Red Hat certified collections. No single tool queries both.
