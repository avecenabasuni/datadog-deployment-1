# Finalization and production readiness

Reviewed 25 September 2026. Repository checks are not production sign-off.
The operator reported successful lab deployment before application rollback was
added. The new rollback still needs the VM exercise below.

## What is implemented

- Stable production/lab entry points and separate environment/configuration paths.
- Compose plugin/legacy selection, unchanged base Compose/.env, original volume
  preservation, schema preflight and pinned running images.
- Explicit application rollback using immutable original image IDs and checked
  mounts, including stopped-container recovery. No volume deletion or data restore.
- Private recovery metadata and a last-stage status file; interruption/error
  reporting without subprocess output or secret values.
- RUM mount persistence, non-secret MySQL config permissions, and lab-only schema
  repair. Installer entry scripts require reviewed hashes.
- Regression tests, real Compose parser checks, and Linux shell line endings.

## Exercise recovery in the dedicated lab

Use the latest code and an already configured rehearsal fixture. Apply must finish
or at least create `recovery.json` before rollback is available. The commands below
recreate containers and deliberately stop the lab API; never use the fault step
on production.

```bash
git pull origin main
sudo bash scripts/eminerba-lab --dry-run
sudo bash scripts/eminerba-lab --maintenance
sudo bash scripts/eminerba-lab rollback --dry-run
sudo docker stop eminerba_api
sudo bash scripts/eminerba-lab rollback --maintenance
curl --fail http://127.0.0.1:8081/health.php
curl --fail http://127.0.0.1:8081/api/health.php
```

Verify the browser's read-only SQL request still returns the same sample records,
the original named volume is attached, and RUM injection is absent. Agent and host
SSI remain installed by design. Then reapply and verify telemetry again:

```bash
sudo bash scripts/eminerba-lab --dry-run
sudo bash scripts/eminerba-lab --maintenance
```

Record outcomes and timestamps, including any failed stage. A dry-run alone does
not prove rollback. SQL credentials must still match the preserved database.

## Production conditions still requiring an operator

1. Tested, current database backup and recovery access; metadata is not a backup.
2. Working base application images; required writable-layer changes exported or
   built into images before recreation. Retain original image tags and mount files.
3. Actual CodeIgniter/database driver, read-only endpoints, authentication, proxy,
   CSP, browser RUM and trace/DBM correlation acceptance.
4. Maintenance window, resource capacity and an assigned recovery operator. All
   three services can experience downtime; this is not a rolling deployment.
5. Reviewed installer entry scripts and downstream behavior. Hashes do not pin all
   downloads. Host SSI/Agent rollback and DBA changes remain separate procedures.
6. No concurrent standalone stages, external Compose jobs or installers. The
   coordinator's lock does not serialize unrelated external commands.

Use [validation](VALIDATION.md), [recovery](ROLLBACK.md), and
[local test evidence](LOCAL-TESTS.md) together. Do not mark production ready until
the applicable checks are recorded on the target-equivalent VM.

Implementation references: Docker's [Compose recreation options](https://docs.docker.com/reference/cli/docker/compose/up/)
and [context precedence](https://docs.docker.com/reference/cli/docker/), and Datadog's
[SSI opt-out and uninstall procedure](https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/docker/).
