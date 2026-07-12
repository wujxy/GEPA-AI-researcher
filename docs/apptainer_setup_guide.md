# GEPA Apptainer Executor — Automatic Image Materialization

## Overview

GEPA runs each executor agent (the Claude Code call that implements/validates a
candidate) inside an Apptainer container when `execution.runtime_backend: apptainer`
is set in the project profile. **You no longer build or configure the image by
hand.** GEPA derives what the container needs directly from the resolved task/profile
config, builds (or reuses) a thin SIF, and validates it by really executing commands
inside it.

This replaces the old interactive `setup_apptainer.py` wizard, which was
Docker-dependent and guessed the image from file extensions.

## What you need

- **Apptainer** installed. Docker is **not** required — GEPA builds via
  `apptainer build out.sif docker://<base>`, which pulls OCI layers straight from
  the registry.
- **Claude Code** resolvable on the host (`agent.command`, default `claude`). The
  host Claude + node are bind-mounted read-only into the container; nothing needs
  to be baked into the image.
- On hosts where Apptainer's setuid helper is unavailable, GEPA auto-detects this
  and runs exec under `--userns` (unprivileged user namespaces).

## How the image is chosen

GEPA scans the commands the executor may run (`environment.setup_commands`,
`metric.command`, `validation.checks[].command`, `runtime.python_command`) and picks:

| Signals in config | Base image | Why |
|---|---|---|
| `/cvmfs/...` referenced, or `gcc`/`g++`/`cmake`/`make` | `docker://almalinux:9` | glibc matches CVMFS-built (el9) binaries; toolchain comes from a read-only `/cvmfs` bind, not the image |
| pure Python only (python/pytest/bash/git) | `docker://python:3.11-slim` | smallest base that already ships python3 |

Override with `execution.apptainer.base_image` (e.g. `docker://rockylinux:9`).

## What gets bound into the container

- The candidate worktree → `/workspace/repo`, artifacts → `/workspace/artifacts`,
  plus per-execution `scratch_<exec_id>` and `home_<exec_id>`.
- The host nvm node-version directory (containing `claude` + `node`) → the **same
  absolute path** read-only, so the host `PATH` resolves `claude` unchanged.
- `/cvmfs` → `/cvmfs` read-only, when a command references it.
- `claude_home_template` → the per-execution HOME (use this for a minimal
  `.claude` config / credentials; never bind the host `$HOME` directly).

`HOME` is set with `--home` (Apptainer silently ignores `HOME` passed via `--env`).

## Usage

```bash
# Build/reuse + validate the image explicitly and print diagnostics:
python -m gepa_researcher.cli setup-apptainer --config examples/omilrec/task.template.yaml
python -m gepa_researcher.cli setup-apptainer --config examples/omilrec/task.template.yaml --force

# Or simply run/validate — the image materializes lazily and is reused from cache:
python -m gepa_researcher.cli validate --config examples/omilrec/task.template.yaml
python -m gepa_researcher.cli run --config examples/omilrec/task.template.yaml --run-dir /tmp/gepa-run
```

The image is cached at `~/.cache/gepa/images/<fingerprint>.sif` (override with
`GEPA_IMAGE_CACHE_DIR`). The fingerprint includes the base, detected tools, and the
host claude binary path + mtime, so upgrading Claude invalidates and rebuilds
automatically. Parallel GEPA runs share the cache safely (file lock + atomic rename).

## Using your own prebuilt image

```yaml
execution:
  runtime_backend: apptainer
  apptainer:
    auto_image: false            # disable auto-materialization
    image: /abs/path/your.sif    # then required
```

`auto_image: false` makes `image:` required (validated by `gepa ... validate`).
GEPA still auto-detects `--userns` so exec works; set `userns: true/false` to
override, and `extra_exec_args: [...]` for any other `apptainer exec` flags.

## Minimal profile snippet

```yaml
execution:
  runtime_backend: apptainer
  apptainer:
    auto_image: true            # default; image is built and cached for you
    command: claude             # resolved inside the container
    cleanenv: true
    containall: true
    writable_tmpfs: true
    container_repo: /workspace/repo
    container_artifacts: /workspace/artifacts
```

## Verification

```bash
# Offline suite (no real apptainer):
python -m unittest discover -s tests -q
# Real-apptainer end-to-end (pulls a tiny image, exercises the real exec path):
GEPA_REAL_APPTAINER=1 python -m unittest tests.test_apptainer_real
```

## Troubleshooting

- **`Image materialization failed: ... starter-suid doesn't have setuid bit set`**
  for *both* default and `--userns` exec — your Apptainer install cannot start
  containers at all. Reinstall Apptainer with setuid, or enable unprivileged user
  namespaces (`sysctl kernel.unprivileged_userns_clone=1` / `singularity config
  --set ...`).
- **Build fails (registry/network)** — the error includes the exact
  `apptainer build ...` command to retry and the `auto_image: false` fallback.
- **A required tool is missing in the image** — set `base_image` to a base that
  includes it (e.g. one with `git` if your executor needs git inside the container;
  normally the orchestrator handles git on the host).
- **`/cvmfs` referenced but not mounted** — GEPA warns and skips the bind; source
  CVMFS on the host first.
