# OMILREC Br1.11 GEPA Examples

This directory contains worked canonical GEPA configs for the OMILREC Br1.11 speed-optimization resource packs.

Canonical examples:

- `br111-local.task.yaml` + `br111-local.project.profile.yaml`: local executor backend.
- `br111-apptainer.task.yaml` + `br111-apptainer.project.profile.yaml`: Apptainer executor backend.

Both examples assume this workspace layout:

```text
omilrec_opt/
├── GEPA-AI-researcher/
├── omilrec-br111-executor-pack/
└── omilrec-br111-executor-pack-apptainer/
```

The project profiles intentionally include:

```yaml
resources:
  pre_materialized_lfs_paths:
    - tests/fixtures/v107_rev1/*.bin
```

That field is required when the controller checkout has real local Git-LFS fixture binaries that must be copied into every per-candidate worktree. Without it, a worktree may contain 131-byte LFS pointer stubs and `test_fcn` can fail with errors such as `bad magic in binary file`.

Validate from the GEPA checkout:

```bash
python -m gepa_researcher.cli validate --config examples/omilrec/br111-local.task.yaml --no-materialize
python -m gepa_researcher.cli validate --config examples/omilrec/br111-apptainer.task.yaml --no-materialize
```

Launch with a fresh run directory after adjusting paths for your machine:

```bash
python -m gepa_researcher.cli run --config examples/omilrec/br111-local.task.yaml --run-dir ../omilrec-br111-executor-pack/runs/run-local-<id>
python -m gepa_researcher.cli run --config examples/omilrec/br111-apptainer.task.yaml --run-dir ../omilrec-br111-executor-pack-apptainer/runs/run-apptainer-<id>
```

`task.template.yaml`, `project.profile.template.yaml`, and the legacy JSON examples are retained as older/template references. Prefer the `br111-*.yaml` files for new canonical runs.
