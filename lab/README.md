# Ubuntu instrumentation lab

For the production layout and one-command deployment, use the newer
[production-layout rehearsal](rehearsal/README.md). This page retains the
original manual staged fixture; do not run both fixtures/Agents on the same VM.

A standalone test stack, not a copy of Sucofindo's deployment. It contains three
services on one Docker network: `eminerba-web` (Apache/PHP 8.1), `eminerba-api`
(Apache/PHP 8.1 with PDO MySQL), and `eminerba-db` (MySQL 8.0). The Datadog Agent
is created separately by the existing automation.

The browser requests `/api/` through the web Apache proxy. The API reads seeded
sample rows with PDO. No PHP tracer or browser RUM SDK is installed in the images.
This exercises instrumentation, not CodeIgniter-specific behavior.

## Safety and prerequisites

- Use a dedicated Ubuntu 22.04/24.04 VM with rootful Docker and either
  `docker compose` or `docker-compose`.
  Start with roughly 4 vCPUs, 8 GB RAM, and 25 GB free disk; adjust after measuring.
- Take a VM snapshot before SSI. SSI changes the host Docker runtime and can affect
  other containers on that VM. Do not share the VM with production workloads.
- PHP 8.1 is intentionally used to match the target; it reached end of life on
  31 December 2025. This is an isolated compatibility lab, not a supported-version
  recommendation. See [PHP unsupported branches](https://www.php.net/eol.php).
- Only `127.0.0.1:8080` is published. MySQL and API ports are not published.
  Use an SSH tunnel for the browser, not a public firewall rule.
- Telemetry uses `env:lab`, dedicated service names, and a separate RUM application.
  A separate API key helps credential management but does not create a separate
  Datadog organization or prevent billing. Review sampling and account limits.
- The generated environment file contains lab database passwords. It is ignored
  by Git and excluded from the Docker build context. Do not print full Compose config.

## 1. Generate configuration

Copy this repository to the VM. Run all commands below from the repository root:

```bash
python3 lab/prepare.py --dry-run
python3 lab/prepare.py
```

This writes private files with random database passwords:

- `lab/.env`: image selectors and database initialization credentials.
- `config/lab.json`: container mappings, `env=lab`, URLs, and separate output paths.
- `config/lab-secrets.json`: Datadog key placeholder, DBM password, and matching MySQL root password.

Reruns refuse existing files rather than rotate passwords. Initialization SQL runs
only on an empty MySQL data volume. Editing passwords in `.env` does not update
accounts in an existing database; preserve generated files between tests.

Review `PHP_IMAGE` and `MYSQL_IMAGE` in `lab/.env`. Initial values are
`php:8.1-apache-bookworm` and `mysql:8.0`, which are moving version-family tags,
not immutable pins. Pull/build on the VM to establish availability, record resolved
digests, and pin approved digests for repeatable tests. If an archived PHP tag is
unavailable, use a trusted approved PHP 8.1 Apache image; do not silently substitute
a newer PHP version. The current workspace has not pulled these images.

## 2. Start the uninstrumented baseline

Define these helpers in the current Bash session. They keep the Compose project,
environment file, and automation configuration consistent:

```bash
dc() {
  sudo bash scripts/compose --env-file "$PWD/lab/.env" -p eminerba-lab \
    -f "$PWD/lab/docker-compose.yml" "$@"
}
dd_lab() {
  sudo bash scripts/datadog-bootstrap "$@" \
    --config "$PWD/config/lab.json" --secrets "$PWD/config/lab-secrets.json"
}
dc config -q
dc build --pull
dc up -d
dc ps
curl --fail --max-time 15 http://127.0.0.1:8080/api/
```

The `scripts/compose` wrapper automatically prefers `docker compose` and falls
back to `docker-compose` when the plugin is unavailable. All `dc` commands below
work with either installation. A failed deployment command is returned directly;
it is not retried with the other Compose implementation.

MySQL health checks allow up to approximately five minutes for first initialization.
If it fails, inspect `dc logs --tail=80 eminerba-db` locally; redact logs before sharing.
Do not continue until the API returns the three seeded samples.

From your workstation, open a tunnel and keep it running:

```bash
ssh -N -L 8080:127.0.0.1:8080 <user>@<ubuntu-vm>
```

Open `http://localhost:8080/` in the workstation browser and click **Run read-only
SQL request**. Before SSI, the diagnostic pages should show a null tracer and no
APM socket connection. Do not expose these lab-only diagnostics publicly.

## 3. Configure Datadog and review installers

Edit `config/lab.json` locally: set a reviewed exact Agent `7.X.Y` image accepted
by preflight, your site, and lab RUM application/client token/remote configuration
ID. In `config/lab-secrets.json`, replace only the API key placeholder with a new
key; keep generated DBM/admin passwords. Optional security/CNM/USM flags default
to false; enable only after reviewing the privileges in the main runbook.

```bash
dd_lab discover
dd_lab preflight
dd_lab fetch-installers
```

Review `/opt/sucofindo-lab/artifacts/ssi.sh` and `rum.sh`, including downstream
downloads. Set their reviewed SHA-256 values in `config/lab.json`.

```bash
dd_lab render --dry-run
dd_lab render
dc -f /opt/sucofindo-lab/generated/application.override.json config -q
dd_lab agent-start --dry-run
dd_lab agent-start
dd_lab ssi-install --dry-run
dd_lab ssi-install --maintenance
```

The Agent may report MySQL check errors until the next stage provisions DBM.
Use only one Agent per VM. An existing Agent or partial SSI installation stops the
automation for review instead of silently replacing it.

## 4. Apply the lab handoff and provision DBM

These are explicit operator actions on the disposable lab, not actions performed
by the production automation. They recreate containers but preserve the named database volume.

```bash
dc -f /opt/sucofindo-lab/generated/application.override.json \
  up -d --force-recreate eminerba-db
dc -f /opt/sucofindo-lab/generated/application.override.json \
  up -d --force-recreate eminerba-api eminerba-web
dc ps
curl --fail --max-time 15 http://127.0.0.1:8080/api/
dd_lab dbm-apply --dry-run
dd_lab dbm-apply --maintenance
curl --fail --max-time 15 http://127.0.0.1:8080/health.php
curl --fail --max-time 15 http://127.0.0.1:8080/api/health.php
```

Confirm HTTP diagnostics show PHP 8.1, the loaded tracer, `env=lab`, the intended
services, and `apm_socket_connected=true`. The API should show propagation `full`.
These diagnostics run as the Apache worker, unlike a root CLI extension check.
The full `verify` stage also requires RUM, so run it after the next stage.

## 5. Install RUM and prove persistence

```bash
dd_lab rum-install --dry-run
dd_lab rum-install --maintenance
dd_lab verify
dd_lab smoke
```

Read the printed export path under `/opt/sucofindo-lab/generated/rum-persistence-*`.
Review its manifest, Apache configuration, module assets, and
`persistence.override.example.json`. For this lab, test the exported bind-mount
alternative; replace the placeholder below with the exact reviewed export directory:

```bash
RUM_EXPORT=/opt/sucofindo-lab/generated/rum-persistence-REPLACE_WITH_ACTUAL_ID
dc -f /opt/sucofindo-lab/generated/application.override.json \
  -f "$RUM_EXPORT/persistence.override.example.json" config -q
dc -f /opt/sucofindo-lab/generated/application.override.json \
  -f "$RUM_EXPORT/persistence.override.example.json" \
  up -d --force-recreate eminerba-web
dd_lab verify
dd_lab smoke
```

Keep both override files in subsequent recreation commands. Recreating from the
base Compose file alone removes configured instrumentation mounts/environment.
Production should use the reviewed image or mount strategy in the
[main runbook](../docs/DEPLOYMENT.md#5-rum-and-persistence), not blindly copy lab exports.

In the lab RUM configuration, set Allowed Tracing URLs for the browser-visible
`http://localhost:8080/api/` (not the Docker service hostname). This same-origin
flow does not need cross-origin CORS changes. Verify sampling settings and browser
trace headers. Click the button several times and confirm the matching backend
trace and PDO SQL span, then DBM query sample correlation. CLI requests do not
generate browser RUM sessions. Account/site support for Apache RUM must be checked.

## 6. Rerun, acceptance, and stopping

Rerun `dd_lab render`, `dd_lab agent-start`, `dd_lab ssi-install --maintenance`,
and `dd_lab dbm-apply --maintenance`: unchanged managed state should be preserved.
Repeat explicit application recreation with both reviewed overrides and rerun
verification. Confirm seed rows remain present and credentials have not changed.
Use the [acceptance checklist](../docs/VALIDATION.md), substituting `lab` for `prod`.
This fixture intentionally tests PDO; it does not establish MySQLi/CodeIgniter compatibility.

To stop without deleting data:

```bash
dc stop
sudo docker stop eminerba-lab-agent
```

Stopping containers does not uninstall host SSI. Follow the
[SSI rollback procedure](../docs/ROLLBACK.md#host-ssi-and-php), or restore the
dedicated VM snapshot under your lab procedure. Do not run volume deletion/pruning
commands. No automatic cleanup or credential rotation is included.

## References and test boundaries

- [Official PHP image](https://hub.docker.com/_/php): Apache variant and extension build helpers.
- [Official MySQL image](https://hub.docker.com/_/mysql): initialization variables and first-run SQL.
- [Datadog Docker SSI](https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/docker/): host installer and runtime recreation.
- [Local test results](../docs/LOCAL-TESTS.md): generation/configuration tests are offline.

Docker image builds, PHP execution, MySQL startup, SSI installation, RUM, and
Datadog telemetry have not been executed in the development workspace. Complete
these checks on your VM; report the failing stage and redacted error if blocked.
