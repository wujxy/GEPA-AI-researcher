# GEPA Apptainer Executor — Automatic Image Materialization

## Overview

GEPA runs each executor agent (the Claude Code call that implements/validates a
candidate) inside an Apptainer container when `execution.runtime_backend: apptainer`
is set in the project profile. Apptainer is an isolation boundary, not a software
distribution environment: GEPA builds (or reuses) a thin boot SIF and passes the
host runtime plus the user-declared runnable envelope into it.

This replaces the old interactive `setup_apptainer.py` wizard, which was
Docker-dependent and guessed the image from file extensions.

## What GEPA bootstraps

- **Apptainer runtime discovery.** GEPA checks `execution.apptainer.executable`,
  `GEPA_APPTAINER`, `apptainer`/`singularity` on `PATH`, and the GEPA runtime
  cache (`~/.cache/gepa/runtime` by default). If none is found, a site can provide
  a pinned user-mode install hook with `execution.apptainer.install_command` or
  `GEPA_APPTAINER_INSTALL_COMMAND`; GEPA runs it once and probes the cache again.
- **Executor SIF.** Docker is **not** required — GEPA builds a thin boot image via
  `apptainer build out.sif docker://<base>`, pulling OCI layers straight from the
  registry, then reuses the SIF from a content-addressed cache.
- **Host runtime passthrough.** GEPA mounts existing host runtime roots read-only
  (`/usr`, `/lib`, `/lib64`, `/bin`, `/sbin`, selected runtime `/etc` paths, and
  `/cvmfs` when present) instead of installing project packages into the SIF.
- **Host capabilities.** GEPA probes whether normal `exec` works or whether
  `--userns` is required, and records diagnostics in the resolved snapshot.
- **Claude Code/auth.** `agent.command` (default `claude`) is resolved on the host
  and the host Claude + node tree is bind-mounted read-only. Claude auth files are
  projected into the per-execution HOME when present.

## How the image is chosen

By default GEPA uses `docker://alpine:3.20` as a thin boot image. The image only
needs to start Apptainer exec and a shell; project compilers, Python, ROOT/JUNO,
pytest, and OS runtime libraries come from host-runtime passthrough and
`provided_paths`. Override with `execution.apptainer.base_image` only when your
site needs a different boot base.

## What gets bound into the container

- The candidate worktree → `/workspace/repo`, artifacts → `/workspace/artifacts`,
  plus per-execution `scratch_<exec_id>` and `home_<exec_id>`.
- The host nvm node-version directory (containing `claude` + `node`) → the **same
  absolute path** read-only, so the host `PATH` resolves `claude` unchanged.
- Host runtime roots (`/usr`, `/lib`, `/lib64`, `/bin`, `/sbin`, selected runtime
  `/etc` paths, and `/cvmfs`) → the same absolute paths read-only, when present.
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
the host runtime bind set and host claude binary path + mtime, so upgrading Claude
or changing runtime passthrough invalidates and rebuilds automatically. Parallel
GEPA runs share the cache safely (file lock + atomic rename).

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
- **A required tool is missing in host-runtime passthrough** — ensure the host
  runtime paths are visible and mounted (`/usr`, `/lib*`, `/bin`, `/sbin`). GEPA
  does not install project packages into the SIF.
- **`/cvmfs` is needed but not mounted** — GEPA skips missing host paths; source
  CVMFS on the host first or declare the correct provided path.
