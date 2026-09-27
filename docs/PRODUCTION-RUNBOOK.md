# Eminerba production deployment runbook

Last reviewed: 27 September 2026.

This runbook installs Datadog infrastructure monitoring, container logs, PHP APM
through host Single Step Instrumentation (SSI), MySQL Database Monitoring (DBM),
and Apache browser RUM auto-instrumentation on the existing Eminerba Docker host.
It uses this repository's production coordinator from preparation to recovery.

**Deployment recreates the database, API and web containers. Schedule downtime.**
The script stops on failure; application rollback is a separate command.
Run the steps in order and inspect each result. Stop on a failed command; do not
paste the entire runbook into a shell as one unattended batch.

The operator reported successful lab deployment, telemetry validation, rollback
and reapplication. This is operator-reported rehearsal evidence, not an observed
production deployment. Customer application behavior must still be verified.

## Contents

1. [Scope and target layout](#1-scope-and-target-layout)
2. [Prerequisites and go/no-go checklist](#2-prerequisites-and-go-no-go-checklist)
3. [Get the approved repository revision](#3-get-the-approved-repository-revision)
4. [Inventory and protect the existing deployment](#4-inventory-and-protect-the-existing-deployment)
5. [Prepare production configuration](#5-prepare-production-configuration)
6. [Complete the configuration and secrets](#6-complete-the-configuration-and-secrets)
7. [Review installers and record checksums](#7-review-installers-and-record-checksums)
8. [Run the deployment preview](#8-run-the-deployment-preview)
9. [Deploy during maintenance](#9-deploy-during-maintenance)
10. [Verify the application and local instrumentation](#10-verify-the-application-and-local-instrumentation)
11. [Validate telemetry and approve the change](#11-validate-telemetry-and-approve-the-change)
12. [Handle failures and perform rollback](#12-handle-failures-and-perform-rollback)
13. [Operate the deployment after acceptance](#13-operate-the-deployment-after-acceptance)
14. [Troubleshooting](#14-troubleshooting)
15. [Handover record and references](#15-handover-record-and-references)

## 1. Scope and target layout

Production must already be deployed and working under:

```text
/opt/eminerba_docker/
├── docker-compose.yml
├── .env
├── docker/
│   ├── DockerFile
│   ├── apache.conf
│   ├── php.ini
│   └── my.cnf
├── pit_to_port/
└── apiminerba/
```

| Role | Compose service | Container | Supplied layout |
| --- | --- | --- | --- |
| Web | `web` | `eminerba_web` | `./pit_to_port:/var/www/html`, PHP session volume, host port 81 |
| API | `api` | `eminerba_api` | `./apiminerba:/var/www/html`, host port 80 |
| Database | `db` | `eminerba_db` | MySQL 8.0, existing data volume, `./docker/my.cnf:/etc/mysql/conf.d/my.cnf` |

The coordinator discovers the actual Compose project and shared network from the
containers. Do not assume the database volume is literally named `mysql_data`:
Compose commonly prefixes it with the project name. The actual existing mount is
validated and retained.

This workflow supports exactly these three Compose services and fixed container
names. Extra services, multiple shared networks during prepare, additional
unrecognized overlays or changed source mappings require review before using it.
The repository is separate from `/opt/eminerba_docker`; it does not replace the
customer Dockerfile, source, `.env`, or base Compose file.

Use `scripts/eminerba-production` throughout this runbook. **Do not run
`eminerba-lab prepare`, `eminerba-lab repair`, or lab cleanup on production.**
Missing production containers or schemas must be restored or reconciled by their
owners; production preparation does not create the business application.

## 2. Prerequisites and go-no-go checklist

### Host and application requirements

| Item | Requirement for this repository |
| --- | --- |
| Host | Ubuntu 22.04 or 24.04 LTS for this runbook; the current preflight also recognizes Ubuntu 20 LTS |
| Architecture | `x86_64`/amd64 or `aarch64`/arm64; verify every selected Agent and installer artifact supports the actual architecture |
| Access | SSH, sudo/root and an out-of-band recovery path |
| Docker | Existing local rootful Engine using `/var/run/docker.sock`; no remote context/rootless daemon |
| Compose | Working `docker compose` preferred; `docker-compose` fallback is supported and requires `python3-yaml` |
| Host tools | Python >=3.8, Bash, curl, tar, coreutils, util-linux, dpkg-query, systemctl; Git/editor for setup |
| Application | Running Apache/PHP 8.1 web and API containers; customer database access already works |
| Database | Existing Oracle MySQL and persistent writable datadir at `/var/lib/mysql`; supplied stack uses 8.0 |
| Capacity | Measured CPU/RAM/disk headroom for Agent, Performance Schema, image builds and exports; size against production load |
| Recovery | Tested database backup/restore procedure, retained application images and source/configuration backups |

These are repository constraints, not a general vendor support matrix. The code
accepts Oracle MySQL 5.7, 8.0 and 8.4; it does not upgrade MySQL or accept every
vendor-supported distribution. Confirm SSI and module compatibility for the
actual OS, architecture, PHP and Apache build before the window.

Install missing host utilities through the normal package-change procedure:

```bash
sudo apt-get update
sudo apt-get install -y git python3 bash curl ca-certificates tar gzip \
  coreutils util-linux nano less
```

For legacy Compose only:

```bash
sudo apt-get install -y python3-yaml
```

An existing production Docker Engine must not be replaced or upgraded as a side
effect of observability setup. If Docker/Compose is missing or broken, have the
platform team restore the existing application first. For an independently
approved installation, use the [official Ubuntu installation procedure](https://docs.docker.com/engine/install/ubuntu/).

The coordinator can build derived web/API images from the exact running image
IDs to add missing curl, CA certificates, GPG, tar, gzip, coreutils and util-linux.
It expects the supplied Debian-based image with a root initial user and working
package repositories. It does not run the customer Dockerfile or Composer install.
PHP/Apache themselves and required application extensions must already work.

### Access, networking and Datadog inputs

- Obtain a production Datadog API key, the correct Datadog site, and access to
  Infrastructure, Logs, APM, DBM and RUM pages for acceptance. A Datadog application
  key is not required by this script.
- Obtain a reviewed exact Agent tag `registry.datadoghq.com/agent:7.X.Y`; the
  repository requires >=7.76.1 and rejects `latest`, `7`, and digest-only values.
  This minimum is a code requirement, not a recommendation to skip newer fixes.
- Create/select the production browser RUM application using the Apache
  auto-instrumentation setup. Obtain its application ID, client token and remote
  configuration ID. The vendor currently describes Apache RUM auto-instrumentation
  as Preview; confirm availability and suitability in your organization.
  See [Apache RUM setup](https://docs.datadoghq.com/real_user_monitoring/application_monitoring/browser/setup/server/apache/).
- Obtain the existing MySQL administrator credentials. The account must be able
  to inspect schemas/users/routines, create the monitoring account/schema/routines,
  issue the required grants and enable the specified Performance Schema consumers.
  The coordinator requires administrative SQL access; if policy requires DBA-only
  provisioning, use the separately reviewed [staged workflow](DEPLOYMENT.md).
- Permit the Agent to reach MySQL directly through the shared Docker network.
  Permit web to reach `http://eminerba-agent:8126/info`, and PHP workers to access
  the shared APM Unix socket. No new public 8126 or 3306 listener is required.
- Provide outbound DNS and HTTPS access for the Agent registry, Datadog intake
  for your site, installer sources, their downstream repositories and OS package
  repositories. Browser clients need access to the applicable RUM CDN/intake.
  Do not disable TLS verification to work around connectivity failures.
- Installer entry URLs currently used by the code are
  `https://install.datadoghq.com/scripts/install_script_agent7.sh` and
  `https://rum-auto-instrumentation.s3.amazonaws.com/installer/latest/install-proxy-datadog.sh`.
  Review downstream URLs; these two hosts alone are not a complete allowlist.
- Select read-only HTTP endpoints reachable from the host without interactive
  login. The current probes use unauthenticated GETs, do not follow redirects,
  and have no custom-header/cookie configuration. Never embed credentials in URLs.
  Select an HTML page for RUM and an API request that actually queries MySQL.

### Required decisions before maintenance

- [ ] The application owner confirms the baseline is healthy and identifies a
  read-only business flow, the actual PHP database driver and public browser origins.
- [ ] The DBA confirms the existing schemas, datadir, account/grants and available
  database backup, including a tested restoration procedure and recovery owner.
- [ ] Required edits in container writable layers have been exported or baked into
  retained images. Container recreation discards unmounted writable-layer changes.
- [ ] Review existing Agent/SSI/RUM/manual tracer installations. One managed Agent
  is expected; unrelated or partial installations require reconciliation first.
- [ ] Confirm no conflicting tracer extensions or active PHP JIT; check the HTTP
  SAPI as well as CLI. Do not remove customer extensions without application review.
- [ ] Review the host-wide SSI effect on other workloads. All external Compose
  jobs, upgrades and installers are paused for the window.
- [ ] Review collection scope, sampling, log/query sensitivity and resource/billing
  impact. Runtime security, network monitoring and USM are separate optional choices.
- [ ] Agree on downtime, rollback triggers, observation interval and sign-off owner.

## 3. Get the approved repository revision

Run on the production host. The examples use the operator's home directory:

```bash
cd ~
git clone https://github.com/avecenabasuni/datadog-deployment-1.git
cd datadog-deployment-1
git status --short
git log -1 --oneline
```

If the repository already exists, update it before the maintenance window:

```bash
cd ~/datadog-deployment-1
git status --short
git pull --ff-only origin main
git log -1 --oneline
```

Resolve local changes without discarding configuration. Record and approve the
revision before deployment; do not pull another revision in the middle of a run.
Run the remaining commands from this repository directory. Authenticate through
your normal Git mechanism if required; do not put access tokens in clone URLs.

## 4. Inventory and protect the existing deployment

Check the host and Docker without displaying container environment values:

```bash
cat /etc/os-release
uname -m
python3 --version
free -h
df -h / /opt /var/lib/docker
sudo docker info --format 'Version={{.ServerVersion}} Runtime={{.DefaultRuntime}} Root={{.DockerRootDir}}'
sudo docker context inspect --format '{{.Endpoints.docker.Host}}'
sudo bash scripts/compose version
sudo docker ps -a --format '{{.Names}} | {{.Image}} | {{.Status}}'
sudo systemctl is-active datadog-agent
```

An inactive/absent host Agent returns nonzero and is normal for this containerized
Agent design. An active host Agent requires an ownership decision before continuing.
Inspect any `DOCKER_HOST`/`DOCKER_CONTEXT` overrides locally; the coordinator checks
the effective target. If Docker stores data elsewhere, check that filesystem's capacity.

Inspect the selected containers and record their project, image IDs and mounts:

```bash
for container in eminerba_web eminerba_api eminerba_db; do
  sudo docker inspect --format \
    '{{.Name}} running={{.State.Running}} image={{.Image}} project={{index .Config.Labels "com.docker.compose.project"}} service={{index .Config.Labels "com.docker.compose.service"}}' \
    "$container"
  sudo docker inspect --format \
    '{{range .Mounts}}{{println .Type .Name .Source "->" .Destination "rw=" .RW}}{{end}}' \
    "$container"
done
```

All three must be running and belong to the expected project. If one is missing,
stop and have the application owner restore it. Do not initialize a replacement
production database to make preflight pass.

Before proceeding, preserve through your normal protected backup system:

1. A consistent database backup with backup ID, timestamp, retention location and
   successful restore-test evidence. Do not copy a live MySQL datadir as a substitute.
2. Base Compose, `.env`, Dockerfile, Apache/PHP/MySQL configuration, application
   release and persistent application data such as uploads and sessions.
3. Original running images, or a tested way to restore those exact images locally.
   Recovery tags created later prevent dangling-image cleanup only; they do not
   protect against all-image pruning, explicit removal or disk loss.
4. Current Docker runtime configuration and access to the host recovery procedure.

`recovery.json` is application recovery metadata, not a database backup. Keep
secret-bearing backups private and do not attach `.env` or full `docker inspect`
or `compose config` output to the change ticket.

## 5. Prepare production configuration

Run once on the existing stack:

```bash
sudo bash scripts/eminerba-production prepare
```

Expected outcome: a `Prepared ...` message naming these files:

```text
config/eminerba.json
config/eminerba-secrets.json
```

Preparation discovers the live network, reads only `DB_NAME`/`DB_NAME2` from the
web container, downloads installer entry scripts, and generates a DBM password.
It does not install the Agent/SSI/RUM or recreate the business application.

If configuration already exists, preparation refuses to overwrite it. Use and
review the matching existing files; do not delete them simply to bypass a refusal.
A partial file pair needs reconciliation. If a download fails before the files
are created, fix connectivity and rerun preparation. For an existing complete
configuration with missing installers, use the explicit fetch command in step 7.

## 6. Complete the configuration and secrets

```bash
sudo nano config/eminerba.json
sudo nano config/eminerba-secrets.json
sudo chmod 600 config/eminerba.json config/eminerba-secrets.json
```

### Settings to fill or confirm

| Field in `eminerba.json` | Required decision |
| --- | --- |
| `agent_image` | Exact reviewed Agent `7.X.Y` tag meeting the repository minimum |
| `site` | Site matching the production Datadog organization and RUM application |
| `version` | Actual application release identifier; keep it consistent with deployed code |
| `mysql_schemas` | Every real application schema to monitor; confirm API schemas as well as discovered web values |
| `db_apps` | `web`, `api`, or both, according to which services issue database queries; preparation defaults to both |
| `rum_application_id` | Production RUM application ID |
| `rum_client_token` | Production RUM browser client token |
| `rum_remote_configuration_id` | ID from the matching Apache auto-instrumentation setup |
| `rum_url` | Host-reachable HTML page with the intended virtual host and no side effects |
| `apm_url` | Host-reachable read-only API endpoint that queries the application database |
| `ssi_sha256`, `rum_sha256` | Approved entry-script hashes from step 7 |
| `timeout` | Integer 1–1800 seconds; default 60; see readiness behavior in step 9 |
| `process_collection` | Default `true`; review process collection scope |
| `runtime_security`, `network_monitoring`, `universal_service_monitoring` | Default `false`; enable only after kernel, privilege, capacity and product-scope review |

`mysql_schemas` must list existing business databases. Do not use `eminerba_lab`
or `eminerba_lab_aux`, create empty customer schemas, or include system schemas
to satisfy preflight. Review actual driver usage without publishing application
database configuration files. Both PDO and MySQLi appear in the current vendor
[MySQL correlation matrix](https://docs.datadoghq.com/database_monitoring/connect_dbm_and_apm/);
confirm the installed tracer/version and prove correlation with real requests.
This package does not change CodeIgniter's driver.

For the supplied ports, local URLs may begin with `http://127.0.0.1:81/` for web
and `http://127.0.0.1:80/` for API. Use the real read-only path and correct virtual
host; do not copy a guessed endpoint. If virtual hosting or TLS requires a domain,
use a reachable URL that routes to the intended application. A redirect/login page
or generic 200 response is not evidence of successful database access.

### Generated values to retain unless explicitly reviewed

| Field/group | Production default |
| --- | --- |
| `env` | `prod` |
| `agent_name` | `eminerba-agent` |
| `web_service`, `api_service` | `eminerba-web`, `eminerba-api` |
| Container and Compose service mappings | Fixed mapping in step 1 |
| `network` | Discovered shared network |
| `mysql_host`, `mysql_port` | `db`, `3306` on the shared Docker network |
| `mysql_data_dir` | `/var/lib/mysql` |
| `mysql_config_target` | `/etc/mysql/conf.d/zz-datadog.cnf` |
| `db_user`, `db_user_host` | `datadog`, `%`; review account matching/reachability with the DBA |
| `admin_user` | `root`; change only to a reviewed account with the necessary administrative rights |
| `artifact_dir` | `/opt/eminerba-observability/artifacts` |
| `output_dir` | `/opt/eminerba-observability/generated` |
| `agent_run_dir` | `/opt/eminerba-observability/agent-run` |
| `socket_dir` | `/var/run/datadog` |
| `docker_socket`, `container_logs_dir` | `/var/run/docker.sock`, `/var/lib/docker/containers`; verify the log path against the daemon |

The commands below assume these default names/paths. Apply any approved changes
consistently to diagnostics and handover. The coordinator requires the specified
datadir and separate `zz-datadog.cnf` target for this production profile.

### Secrets

| Field in `eminerba-secrets.json` | Value |
| --- | --- |
| `api_key` | Production Datadog API key |
| `admin_password` | Existing password for the configured MySQL administrator |
| `db_password` | Retain the generated value for a new DBM account; for an existing account use its real password |

The script does not rotate existing DBM passwords. An existing account must match
the configured host selection and connection limit of 5; conflicting routines
or grants require DBA review. Credentials go to SQL through stdin, not password
arguments. Root/Docker administrators can still inspect runtime processes and files.

The repository's SQL provisions `REPLICATION CLIENT` and `PROCESS` on `*.*`,
`SELECT` on `performance_schema.*` and `mysql.innodb_index_stats`, and `EXECUTE`
on its explain/consumer procedures. It creates the `datadog` schema and explain
procedures there and in each configured application schema. Procedures use
`SQL SECURITY DEFINER`; the DBA must review the administrator/definer's rights and
retention. Existing routine definitions/signatures are checked before changes.

Validate JSON syntax without printing either file:

```bash
sudo python3 -m json.tool config/eminerba.json >/dev/null
sudo python3 -m json.tool config/eminerba-secrets.json >/dev/null
```

## 7. Review installers and record checksums

Read both downloaded scripts and review the dependencies they fetch:

```bash
sudo less /opt/eminerba-observability/artifacts/ssi.sh
sudo less /opt/eminerba-observability/artifacts/rum.sh
sudo sha256sum /opt/eminerba-observability/artifacts/ssi.sh \
  /opt/eminerba-observability/artifacts/rum.sh
```

Press `q` to exit `less`. Copy the approved hashes into `ssi_sha256` and
`rum_sha256` in `config/eminerba.json`. A hash verifies that the reviewed entry
script has not changed; it does not pin its downstream downloads. The configured
`php:1` SSI library selector is a major-version selector, not an exact patch pin.

Only when installer files are missing and the configuration already exists:

```bash
sudo bash scripts/datadog-bootstrap fetch-installers \
  --config config/eminerba.json \
  --secrets config/eminerba-secrets.json
```

Fetch preserves existing installer files. Do not delete files or replace hashes
just to bypass checksum failures; review the intended installer revision first.
Do not separately execute the vendor installation commands copied from its UI:
the coordinator supplies and checks the installer arguments during maintenance.

## 8. Run the deployment preview

```bash
sudo bash scripts/eminerba-production --dry-run
```

Expected: the discovered project, a deployment plan, and a message that no
deployment changes were made. Exit status must be zero.

The preview performs read-only discovery, schema/credential checks, Compose/live
environment comparisons, mount validation and installer checksum verification.
It does not build images, install software, recreate containers or prove browser
telemetry. It takes the shared host lock and performs read probes.

Stop if it reports a mismatch. Resolve missing schemas with the DBA, changed
environment with the application owner, or existing Agent/SSI/RUM ownership with
the platform owner. Do not run maintenance simply to see whether a failed preview
will resolve itself. Docker builds, downstream installer compatibility and actual
RUM injection remain unproven until deployment/acceptance.

## 9. Deploy during maintenance

Confirm the backup, baseline request results, approved revision and recovery
operator are available. Use a stable operator session and run:

```bash
sudo bash scripts/eminerba-production --maintenance
```

The coordinator performs these stages in order:

| Stage | Action and checkpoint |
| --- | --- |
| Preflight | Revalidate project, schemas, credentials, mounts, network and ownership |
| Recovery/render/images | Save `recovery.json`; render private settings; retain original image IDs; build tools-only derived images if needed |
| Agent and SSI | Start/reuse the managed container Agent; install/reuse Docker SSI on the host |
| Database | Recreate `db` with its existing data mount and additional MySQL config; wait for SQL; check startup settings; provision DBM |
| API and web | Recreate with SSI settings/socket; wait for listeners and configured HTTP endpoints |
| RUM | Back up Apache, run the checked installer, disable Apache module APM tracing, test/reload, export persistent assets, recreate web |
| Verify/smoke | Recheck readiness, local instrumentation and HTTP/RUM markers |

The new MySQL config is non-secret mode 0644 and mounted read-only at
`/etc/mysql/conf.d/zz-datadog.cnf`. The original `my.cnf` remains mounted. It enables
Performance Schema and sets the three digest/text lengths to 4096. DBM provisioning
adds monitoring grants and explain/consumer procedures; it does not initialize
application schemas or rotate application credentials.

Recreation uses the discovered project with `--no-deps --no-build --force-recreate`.
Original mount mappings and source availability are checked before recreation.
The base Compose/environment must remain unchanged. Image-building earlier in the
flow is limited to missing tools on top of existing application image IDs.

Database/application/HTTP readiness deadlines are `max(300, timeout)` seconds per
check, with individual probes capped at 10 seconds. Agent readiness uses the
configured `timeout`. Builds and host SSI may run up to 1800 seconds; RUM's
in-container installer has a 600-second deadline plus termination allowance.
Timeouts may include bounded process-cleanup time. These are limits, not a promised
total deployment duration.

Human-readable logs go to stderr with timestamps and `STEP`, `INFO`, `OK`, `PLAN`,
`ERROR`, `DETAIL`, `NEXT` and `STOP` labels. Structured verification JSON goes to
stdout. Failed commands show sanitized diagnostics and an exit code when available.
Do not collect raw environment/configuration dumps as a troubleshooting shortcut.

Expected completion:

```text
[OK   ] Local deployment checks passed.
```

This message is followed by remaining telemetry checks and the override path.
If the command fails or is interrupted, follow step 12 instead of continuing the
remaining stages manually. No automatic rollback or volume deletion occurs.

## 10. Verify the application and local instrumentation

Check containers, Agent health and runtimes:

```bash
sudo docker ps --format '{{.Names}} | {{.Status}}'
sudo docker exec eminerba-agent agent health
sudo docker exec eminerba-agent agent status
sudo docker info --format '{{.DefaultRuntime}}'
sudo docker inspect --format '{{.Name}} {{.HostConfig.Runtime}}' eminerba_web eminerba_api
```

Expected: the managed Agent is healthy; Docker default and web/API runtime are
`dd-shim`. Inspect Agent status locally for APM, logs, forwarder and enabled optional
features; section presence alone does not prove delivery. Review/redact diagnostics
before sharing. Container log output may include application data or credentials.

Recheck the recorded database mount and Apache syntax/module:

```bash
sudo docker inspect eminerba_db \
  --format '{{range .Mounts}}{{println .Type .Name .Source "->" .Destination}}{{end}}'
sudo docker exec eminerba_web apache2ctl -t
sudo docker exec eminerba_api apache2ctl -t
sudo docker exec eminerba_web apache2ctl -M
```

The original database data source must match the pre-window record. Apache syntax
must pass and the web module list must contain `datadog_module`.

To repeat local verification without a full maintenance reapply:

```bash
sudo bash scripts/datadog-bootstrap verify \
  --config config/eminerba.json \
  --secrets config/eminerba-secrets.json

sudo bash scripts/datadog-bootstrap smoke \
  --config config/eminerba.json \
  --secrets config/eminerba-secrets.json
```

These checks require the completed installation. `verify` also invokes diagnostic
SQL procedures and Agent checks; use `smoke` for the narrower HTTP checks. Always
pass the production profile explicitly: standalone defaults refer to different
configuration files. Verify PHP instrumentation from real HTTP requests, not only
CLI extension output.

Ask the application owner to check login, a read-only business request, API access,
sessions and uploads that should remain available. Compare error rates, latency,
CPU, RAM and disk against the baseline for the agreed observation interval.

## 11. Validate telemetry and approve the change

Open the real production page in a browser and execute a bounded set of approved
read-only requests that reach MySQL. Record timestamps without customer request
bodies or personal data. Infrastructure/APM use `env:prod` and the configured
service names; ensure RUM remote configuration also uses the intended production
service/environment/version. The wrapper does not infer all browser SDK settings.

| Product/check | Evidence required |
| --- | --- |
| Infrastructure | Correct host/container inventory and metrics |
| Logs | Expected web/API/database log sources arrive without unexpected sensitive content |
| APM | HTTP request trace for the real application, with database child spans |
| DBM | Correct MySQL instance, query metrics/samples and safe-query explain results |
| RUM | Real browser session/view/resource for the intended production application |
| RUM–APM | A RUM resource opens the matching backend trace |
| APM–DBM | A database span/query sample can be correlated for the actual driver and tracer |
| Optional features | Each enabled process/network/USM/security feature is healthy and visible |

Configure RUM Allowed Tracing URLs for the browser-visible API origins/paths,
agreed sampling and propagators. If web uses port 81 and API uses port 80, those
are different browser origins; inspect actual public routing. CORS and proxies
must permit/preserve the trace headers used. RUM injection must survive CSP,
response content type and compression, with no duplicate manually embedded SDK.

Sampling may omit individual traces or query samples. Use bounded test traffic
and the agreed observation window, not an unbounded request loop. A successful
local smoke check alone does not establish RUM sessions or cross-product correlation.
Complete the detailed [acceptance checklist](VALIDATION.md) before sign-off.

## 12. Handle failures and perform rollback

### Stop, inspect, then choose recovery

Read the error stage, exit code, sanitized detail and suggested next step. Inspect
the status record if deployment had already reached the journaling stage:

```bash
sudo cat /opt/eminerba-observability/generated/deployment-status.json
sudo docker ps -a --format '{{.Names}} | {{.Status}}'
```

The status file may not exist after an early failure, or may describe a previous
run. Correlate its timestamp with the console output. A power loss/SIGKILL can leave
`running`; that is an unknown final outcome. Handled failure/interruption status
is written before the script releases its host lock.

One lock serializes Python deployment commands. External Docker/Compose commands
do not acquire it. Never delete a lock file to bypass an active run. A disconnected
Docker client may leave the bounded RUM installer running in the container until
its deadline; inspect the failed operation before another installation/recovery.

### Application rollback

Use when the change requires reverting application containers and prerequisites
are available. Do not deliberately stop production services to rehearse failure.

```bash
sudo bash scripts/eminerba-production rollback --dry-run
```

If the preview passes and rollback is the chosen maintenance action:

```bash
sudo bash scripts/eminerba-production rollback --maintenance
```

Rollback requires the matching `recovery.json`, original local images, base
Compose/`.env`, existing bind sources/data volume and valid administrator SQL
credentials. It supports missing/stopped application containers, but refuses to
create an empty replacement for a missing data volume.

It recreates DB first with the original data mount, waits for SQL, then recreates
API/web using the recorded image IDs and `runc`. Tracing is disabled and generated
MySQL/socket/RUM mounts are omitted. The latest database contents are retained.

Expected message: `APPLICATION ROLLBACK COMPLETED`. Recheck business requests,
the original volume, Apache configuration and resource/error metrics.

Rollback does **not** remove the managed Agent, host SSI, DBM SQL objects/grants,
database transactions, changes to bind-mounted application files or Datadog-side
settings. It also cannot restore unrecorded container writable-layer edits.
Use the [component recovery procedures](ROLLBACK.md) for those changes. If Docker
cannot start, recover its host runtime first; application rollback needs Docker.

If metadata, original images or data are missing, stop and use the approved manual
recovery/backup procedure. Do not run apply to manufacture a baseline or modify
recorded hashes to bypass a mismatch.

### Reapply after a successful rollback

After correcting the failure cause and scheduling the required maintenance:

```bash
sudo bash scripts/eminerba-production --dry-run
```

Only after a passing preview:

```bash
sudo bash scripts/eminerba-production --maintenance
```

Repeat both application and telemetry acceptance. Reapply is another container
recreation, not a read-only retry.

## 13. Operate the deployment after acceptance

Retain the following protected files and directories for operation and recovery:

| Path | Purpose |
| --- | --- |
| Repository `config/eminerba*.json` | Production settings and private credentials |
| `/opt/eminerba-observability/artifacts/` | Reviewed installer entry scripts |
| `generated/recovery.json` | Immutable original image/mount/project baseline |
| `generated/production-state.json` | Pinned image aliases and RUM persistence state |
| `generated/deployment-status.json` | Last recorded stage/status |
| `generated/production.override.json` | Required observability overlay for subsequent operations |
| `generated/rollback.override.json` | Recovery overlay after application rollback |
| `generated/rum-persistence-*`, `rum-before-*`, `ssi-before-*` | Runtime mounts/exports and component backup records |
| `generated/mysql.d/conf.yaml`, `99-datadog.cnf` | Agent DBM credentials/config and non-secret MySQL startup config |
| `/opt/eminerba-observability/agent-run/` | Agent runtime state |

`generated/` above means `/opt/eminerba-observability/generated/`. Apache exports
may contain configuration secrets. Do not publish them or delete directories
that are still bind-mounted into running containers.

Subsequent Compose operations must use the original project, base file and the
appropriate generated override. Here is a **read-only validation example** for
the instrumented deployment:

```bash
PROD_PROJECT="$(sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' eminerba_web)"
test -n "$PROD_PROJECT" && test "$PROD_PROJECT" != '<no value>' && \
sudo bash scripts/compose \
  --project-directory /opt/eminerba_docker \
  --env-file /opt/eminerba_docker/.env \
  -p "$PROD_PROJECT" \
  -f /opt/eminerba_docker/docker-compose.yml \
  -f /opt/eminerba-observability/generated/production.override.json \
  config -q
```

After rollback, use `rollback.override.json` instead of `production.override.json`;
never combine the two. Full `config` output can contain resolved secrets; use `-q`
for validation. Explicit files/project avoid changing volume names or resolving
relative paths against the wrong directory. See [Compose merge rules](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/).

Do not use base Compose alone to recreate instrumented services: it can remove
observability mounts/settings. Do not use `down -v`, volume pruning, or image
pruning as deployment/cleanup steps. Stop/restart is not equivalent to recreation
when image, environment, mount or SSI settings change.

An application release upgrade, Agent configuration/API key change, different
RUM IDs, or changed base Compose/`.env` requires a reviewed migration. Existing
state checks deliberately reject some changes. The overlay pins images; blindly
rebuilding the customer Dockerfile does not update those pins. Mounted Apache
exports can also hide newer image configuration. Review new images, exported
Apache assets and the recovery baseline together; do not delete state to force
an upgrade through this runbook.

## 14. Troubleshooting

| Symptom | Action |
| --- | --- |
| `no such object: eminerba_web` or another production container | Stop. Verify names/project and have the application owner restore the existing deployment; never use lab prepare on production |
| Configuration already exists | Review the matching config/secrets; preparation intentionally does not rotate them |
| Missing application schema | Compare configuration to the real databases with the DBA; do not create dummy schemas or use lab repair |
| MySQL access denied | Check the configured administrator/DBM credentials and account host/grants locally; do not rotate the application password |
| Compose environment/mount/project differs | Reconcile the actual deployment, `.env`, shell overrides and base Compose; do not bypass hashes |
| Another deployment is active | Identify/wait for the owner; inspect a stuck process before recovery; do not delete lock files |
| Existing Agent or partial SSI | Resolve ownership/installation state using the component runbook; do not install a second Agent blindly |
| Checksum mismatch | Review the intended artifact; do not simply copy the new hash to silence validation |
| Image tools build fails | Inspect sanitized build errors, package mirror/DNS/TLS access, disk capacity and base-image package support |
| MySQL startup values remain 1024 | Check the additional `.cnf` mount and mode 0644; verify effective settings and configuration precedence; another maintenance recreation may be needed |
| Readiness timeout | Inspect the last diagnostic, container state, appropriate local logs and HTTP routing; check database authentication and read-only endpoint suitability |
| RUM installer lacks a required flag | Stop and review the downloaded/downstream configurator version; do not remove the compatibility check |
| Apache syntax or RUM persistence failure | Preserve `rum-before-*`; inspect includes/module dependencies; use explicit recovery if needed before further recreation |
| Missing recovery image/volume/metadata | Stop automated rollback and use the retained artifacts or approved recovery procedure; do not initialize an empty replacement |
| CLI verify passes but UI is empty | Check site/API key, intake connectivity, real traffic, browser consent/sampling/CSP and actual HTTP instrumentation |
| RUM/APM or APM/DBM link missing | Check public URL/CORS/trace headers, sampling, driver and tracer support; installed extensions alone do not prove correlation |

Useful local diagnostics, choosing only the relevant component:

```bash
sudo docker logs --tail 100 eminerba_db
sudo docker logs --tail 100 eminerba_web
sudo docker logs --tail 100 eminerba_api
sudo docker exec eminerba-agent agent check mysql --json
sudo docker exec eminerba-agent agent status
```

These direct commands bypass the wrapper's redaction. Inspect locally and redact
before sharing; avoid pasting complete application logs or credentials into tickets.

## 15. Handover record and references

Record in the change ticket without secret values:

| Evidence | Record |
| --- | --- |
| Change, operator and maintenance times | Ticket ID, responsible people, start/end timestamps |
| Code and application | Repository commit, customer application release |
| Existing deployment | Compose path/project, network, original container image IDs, exact data mount |
| Backup/recovery | Database backup ID and restore evidence, image/configuration retention location |
| Installed components | Exact Agent image ID/tag, installed SSI/PHP/module versions, installer entry hashes |
| Local results | Deployment exit status, final status timestamp, verify/smoke results |
| Business validation | Baseline and post-change login/read-only request results, error/latency observations |
| Datadog validation | Host/service/RUM app identifiers and links to representative traces/query samples |
| Recovery/retention | Override path, recovery baseline availability, assigned operator and retention period |
| Decision | Accepted, pending evidence, or rolled back, with owner and reason |

This document was checked against the repository's command parsers and control
flow. Context7 was used for Docker Compose and Datadog documentation; current
vendor source pages were also checked for Apache RUM and PHP/MySQL correlation.
Do not substitute a vendor example command for the coordinator's reviewed flow.

- [Docker Compose file merging](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/) and [up options](https://docs.docker.com/reference/cli/docker/compose/up/).
- [Docker SSI](https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/docker/) and [SSI compatibility](https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/compatibility/).
- [Self-hosted MySQL DBM](https://docs.datadoghq.com/database_monitoring/setup_mysql/selfhosted/) and [APM–DBM correlation](https://docs.datadoghq.com/database_monitoring/connect_dbm_and_apm/).
- [Apache RUM auto-instrumentation](https://docs.datadoghq.com/real_user_monitoring/application_monitoring/browser/setup/server/apache/).
- [Detailed validation](VALIDATION.md), [component rollback](ROLLBACK.md), [local test evidence](LOCAL-TESTS.md), and [production coordinator overview](EMINERBA-PRODUCTION.md).
