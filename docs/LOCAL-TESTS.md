# Local test results

## Finalization and explicit recovery — 25 September 2026

95 tests ran locally: 94 passed and one POSIX symlink test was skipped on Windows.
Bash entry-point syntax, Python 3.8 grammar compatibility, local Markdown link
targets, and CLI help checks passed. Python 3.8 runtime execution and legacy
Compose 1.29 recovery have not been exercised locally.

The recovery regression suite covers immutable baseline retention, stopped
containers, changed Compose/environment, foreign containers, mount drift, missing
volumes/images, local Docker endpoint selection, maintenance gating, and stopping
before application recreation when database recovery fails. A real Docker Compose
parser checks the generated recovery overlay, image IDs, runtime and retained
mounts. Container recreation, SQL readiness, and Apache execution remain mocked.
The operator reported success for the earlier lab apply; the new rollback has not
yet been executed on that VM. Follow [the recovery rehearsal](RELEASE-READINESS.md).

## Lab auxiliary schema repair — 25 September 2026

83 tests ran locally: 82 passed and one POSIX symlink test was skipped on Windows.
Tests cover schema checks before deployment mutation, missing-schema diagnostics,
lab-only repair guards, read-only repair preview, and insertion of missing sample
rows without replacing existing data. SQL execution is mocked; repair still needs
validation on the lab VM. Go template and real Docker Compose parser tests ran.

## Production-layout lab — 25 September 2026

77 tests ran locally: 76 passed and one POSIX symlink test was skipped on Windows.
The lab tests cover generated layout, credential preservation, refusing existing
workloads/production names, lab versus production profile isolation, reuse of the
production coordinator, and preservation of an already prepared baseline. Real
Docker Compose config parsing checked the generated fixture, named volume, and
database credential mapping without a daemon. Bash syntax and CLI help passed.
PHP image builds, MySQL initialization, SSI/RUM installation, and live telemetry
for this new fixture have not been executed in this development workspace.

## Eminerba production coordinator — 25 September 2026

Final local suite: 69 tests run, 68 passed, one POSIX symlink test skipped on
Windows. Go template tests and the real Docker Compose 5.0.2 configuration parser
test both ran. Bash syntax and CLI help checks passed.

The new coordinator is covered by offline tests for the supplied web/api/db
mapping, live project and mounts, preserved MySQL volumes, mid-run Compose drift,
maintenance gating, Agent ownership, pinned images, missing-tool image builds,
RUM persistence on reruns, bounded DB readiness, and failure before mutation.
An optional integration test uses the installed Docker Compose parser against
temporary fixtures to verify actual override merging without a Docker daemon.
The supplied production Compose/Dockerfile were used as input context, not copied
over the production files. No production connection/deployment has been performed.

## MySQL startup configuration permissions — 25 September 2026

- Windows suite: 48 tests run, 47 passed, one POSIX symlink test skipped.
- Rendering sets the non-secret `99-datadog.cnf` to 0644 so the container's
  MySQL user can read it. Rerender repairs older 0600 files in place, preserving
  their bind-mounted inode; credential-bearing files retain private permissions.
- Tests cover fresh/rerender chmod calls and inode preservation. Actual POSIX
  permissions and database restart behavior still require Ubuntu validation.

## RUM help output regression check — 25 September 2026

- All 46 tests passed locally with Go available.
- RUM installer help validation now reads both stdout and stderr. Other commands
  continue to return stdout only, and command failures remain redacted.
- Tests cover help flags on stdout, stderr, or split across both; missing required
  flags still stop installation and reload. Real installer behavior and RUM
  deployment still need to be checked on the lab VM.

## Discovery template regression check — 25 September 2026

- All 44 tests passed on Windows with Python 3.12 and Go available.
- Inspect expressions now group label lookups before JSON encoding, fixing
  `wrong number of args for json: want 1 got 3` during discovery/preflight.
- The new regression test executes the generated templates with Go's template
  engine for populated, empty, and null label maps. It skips when Go is absent;
  Go is only a test dependency, not a deployment prerequisite.
- Docker deployment on the Ubuntu VM still requires validation.

Last checked: 23 September 2026, using a Windows workspace, Python 3.14, and
Git Bash. These tests did not involve deployment, SSH access, production
credentials, or a Docker daemon.

## Results

- `python -m unittest discover -s tests -p 'test_*.py'`: 43 tests passed, including
  lab configuration generation, no credential rotation, dry-run, partial-state refusal,
  environment routing, and fixture safety checks.
- Bash syntax checks (`bash -n`): the entry point and both test scripts passed.
- Tests use temporary directories and a mocked command runner. Coverage includes
  input validation, secret escaping, dry-run, rerun, existing installation/object
  conflicts, timeouts, checksums, and installer/configuration-test failures.

## Not established by local tests

- Real SSI installation, kernel/OS/image compatibility, and injection into the HTTP SAPI.
- SQL/procedure execution on real MySQL 5.7/8.0/8.4 and production account grants.
- Real Agent health, eBPF/runtime security, and log/process data delivery.
- Downstream RUM installer behavior, module persistence after recreation, and browser HTML.
- Trace ingestion, RUM sessions, and RUM–APM/APM–DBM correlation in Datadog.
- Merging snippets into the team's Compose deployment and completing two maintenance/recreation cycles.
- Building/running the new lab Docker images and linting/executing PHP on the Ubuntu VM.

Use production-equivalent staging and the [validation checklist](VALIDATION.md)
before sign-off. Passing unit tests does not establish production deployment success.
