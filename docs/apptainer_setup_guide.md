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

## What GEPA bootstraps

- **Apptainer runtime discovery.** GEPA checks `execution.apptainer.executable`,
  `GEPA_APPTAINER`, `apptainer`/`singularity` on `PATH`, and the GEPA runtime
  cache (`~/.cache/gepa/runtime` by default). If none is found, a site can provide
  a pinned user-mode install hook with `execution.apptainer.install_command` or
  `GEPA_APPTAINER_INSTALL_COMMAND`; GEPA runs it once and probes the cache again.
- **Executor SIF.** Docker is **not** required — GEPA builds via
  `apptainer build out.sif docker://<base>`, pulling OCI layers straight from the
  registry, then reuses the SIF from a content-addressed cache.
- **Host capabilities.** GEPA probes whether normal `exec` works or whether
  `--userns` is required, and records diagnostics in the resolved snapshot.
- **Claude Code/auth.** `agent.command` (default `claude`) is resolved on the host
  and the host Claude + node tree is bind-mounted read-only. Claude auth files are
  projected into the per-execution HOME when present.

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
# Host/runtime diagnostics; does not build the executor image.
gepa doctor
gepa doctor --config examples/omilrec/task.template.yaml

# Normal path: runtime discovery + SIF materialization happen lazily.
gepa validate --config examples/omilrec/task.template.yaml
gepa run --config examples/omilrec/task.template.yaml --run-dir /tmp/gepa-run

# Explicit image diagnostics entry point; not a required setup step.
gepa setup-apptainer --config examples/omilrec/task.template.yaml
gepa setup-apptainer --config examples/omilrec/task.template.yaml --force
```

If Apptainer is missing and a pinned install hook is configured, run
`gepa doctor --install` or `gepa setup-apptainer --install --config ...` to let
GEPA execute that hook before probing again.

The image is cached at `~/.cache/gepa/images/<fingerprint>.sif` (override with
`GEPA_IMAGE_CACHE_DIR`). Runtime executables discovered or installed by site hooks
live under `~/.cache/gepa/runtime` by default (override with
`GEPA_RUNTIME_CACHE_DIR`). The fingerprint includes the base, detected tools, and
the host claude binary path + mtime, so upgrading Claude invalidates and rebuilds
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

- **`Apptainer runtime was not found`** — install Apptainer, set
  `GEPA_APPTAINER`, set `execution.apptainer.executable`, or provide a pinned
  `install_command` for your site.
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
