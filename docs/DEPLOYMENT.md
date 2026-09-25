# Datadog deployment runbook

[Back to README](../README.md). Run every command below from the package root,
not from the `docs` directory.

This page describes the staged `datadog-bootstrap` workflow. The separate
[Eminerba production coordinator](EMINERBA-PRODUCTION.md) explicitly automates
container recreation for the supplied `/opt/eminerba_docker` stack.

This package stages maintenance changes for Ubuntu, Docker, Apache/PHP 8.1,
CodeIgniter, and MySQL. Work on the host through SSH with the Sucofindo team.
The main Compose file is not required in this workspace. The script does not edit
it, recreate application/database containers, or run `docker-compose down`.

## Method and documentation review

Reviewed on 22 September 2026. Official [Docker SSI documentation][ssi] uses a
**host installer**, `DD_APM_INSTRUMENTATION_ENABLED=docker`, and
`DD_NO_AGENT_INSTALL=true` with a containerized Agent. This package preserves
development's `DD_APM_INSTRUMENTATION_LIBRARIES=php:1`. There is no manual tracer
installer, tracer Dockerfile, or PHP startup wrapper; the previous manual installation files were replaced.

SSI must configure Docker's default runtime as `dd-shim`. New instrumentation
requires **container recreation**, not just a restart. The `php:1` selector pins
the SDK major version, not an immutable patch; installation may fetch newer minor
versions. Inventory records package versions/paths for auditing.

The reviewed [SSI compatibility matrix][compat] lists Ubuntu 20/22/24 LTS,
amd64/arm64, and PHP SDK >=1.6.0. Xdebug, ionCube, NewRelic, Blackfire, pcov, and JIT
may prevent injection. Preflight checks PHP CLI; the team must also check the web
SAPI because configuration can differ. The package does not force `DD_INJECT_FORCE`.
[PHP 8.1 is supported][php]; this package requires an exact Agent 7.X.Y >=7.76.1,
following that page's trace-metadata security recommendation. This intentionally
differs from development's moving `:7` tag.

[APM–DBM correlation documentation][correlation] lists PDO for PHP. APM supports
MySQLi tracing, but MySQLi availability or propagation variables alone do not prove
correlation. Verify the actual CodeIgniter request driver and SQL spans.
The package does not change application drivers.

## Package and prerequisites

- `scripts/datadog-bootstrap`: Bash entry point using Python >=3.8 and its standard library.
- `config/production.example.json`: non-secret settings, container mappings, and feature flags.
- `config/secrets.example.json`: new API key, DBM password, and optional admin password.
- `datadog/templates/`: Agent, application environment, and DBM procedure templates.
- `datadog/mysql/99-datadog.cnf`: startup settings for supported MySQL versions.
- `docs/VALIDATION.md` and `docs/ROLLBACK.md`: acceptance and component rollback.
- `tests/test_deploy.py`: offline mocked tests without Docker/server access.

Host tools: Python 3, Bash, local rootful Docker CLI/daemon, curl, tar, sha256sum,
dpkg-query, and systemctl. SSI installation requires root; Docker socket access
requires administrator privileges. Use `sudo bash scripts/compose` to automatically
select **`docker compose`**, with **`docker-compose`** as a fallback.
Agent automation uses Docker CLI directly and does not depend on the team's Compose
file. Remote/rootless Docker daemons are rejected to avoid installing SSI on a
different host from the containers. Also review container distribution/architecture compatibility.

Web containers require PHP CLI 8.1, Apache (`apache2ctl`, `apachectl`, or `httpd`),
curl, tar, gzip, gpg, and sh. The database container requires `mysqld` and the
`mysql` client. Preflight does not install missing packages; the team adds them to its images.

## 1. Prepare local configuration on the host

```bash
cp config/production.example.json config/production.json
cp config/secrets.example.json config/secrets.json
chmod 600 config/secrets.json
bash scripts/datadog-bootstrap discover
```

Populate configuration using discovery results. The production example keeps
`env=prod`; the environment is configurable
for isolated labs and staging and is propagated to applications, Agent, labels, and DBM tags.
Container and network names must
be explicit; the package does not guess. Discovery includes Compose service/project
labels. Without labels, supply `*_compose_service`; container names are not treated
as service names. Rendering stops for containers from different projects so the
team can prepare per-project snippets. Default paths are examples to verify against the host.

Discovery/preflight only read host/container state and run connection probes.
They do not automatically write reports; rendering saves selected discovery data
in the output directory. Container environments are never dumped. Review internal
reports before sharing them even when they contain no environment or credentials.

Enter a new production API key locally; credentials from conversations are not used.
Leave `admin_password` empty when using the DBA path. JSON is not executed as shell
and does not expand dollar signs, backslashes, or command substitutions. Use normal
JSON escaping for backslashes in passwords. Secrets are not printed, but Docker/root
operators can still inspect runtime credentials.

```bash
bash scripts/datadog-bootstrap preflight
bash scripts/datadog-bootstrap fetch-installers --dry-run
bash scripts/datadog-bootstrap fetch-installers
```

Review `ssi.sh` and `rum.sh` in `artifact_dir`, then set `ssi_sha256` and
`rum_sha256` to the reviewed hashes. Downloading does not execute scripts. Reruns
preserve existing artifacts; hashes are checked again before execution.

Datadog installers download additional components. An entry-script checksum **does
not pin all transitive dependencies**. Record downstream versions/hashes or use
approved mirrors under the team's change-control process. Shell installer hashes
alone do not make this deployment reproducible.

The [official RUM installer][ruminstaller] uses a separate configurator. The reviewed
entry script supports `--agentUri` and forwards other arguments to the configurator.
At installation time, the package runs configurator `--help` and requires
`proxyKind`, `appId`, `site`, `clientToken`, `remoteConfigurationId`, and
`agentUri` before proceeding. Changed flags stop the stage. Review the production
RUM UI command as the final source of parameter values.

## 2. Render and review changes

```bash
bash scripts/datadog-bootstrap render --dry-run
bash scripts/datadog-bootstrap render
```

Preflight determines the MySQL version before rendering. The package accepts Oracle
MySQL 5.7, 8.0, and 8.4; other distributions/versions require review instead of assumed
equivalence. The recommended [self-hosted DBM settings][dbm] are the same for these
branches. The package does not upgrade MySQL. Review Performance Schema memory limits
and database backups before maintenance. Persistent mounts must cover
`mysql_data_dir`; the DBA confirms `@@datadir`, including custom paths.

Outputs in `output_dir` use file mode 0600 and directory mode 0700, except the
non-secret MySQL startup `.cnf`, which is 0644 for the MySQL container user:

- `application.override.json`: JSON accepted as YAML/Compose, using discovered service
  labels, environment variables, socket mounts, and MySQL mounts; no passwords/API key.
- `99-datadog.cnf`: Performance Schema ON, three digest/text limits of 4096,
  current statements/waits, and history consumers.
- `mysql.d/conf.yaml`: JSON accepted as Agent YAML, with DBM enabled and its password.
- `agent-spec.json`: image, network, non-secret environment, mounts, and capabilities for review.
- `discovery.json`: selected discovery snapshot including image/container IDs.

Rerendering changed Agent specifications/password configuration is refused. Use a
new versioned output directory and a reviewed migration. Identical mount files retain
their inodes. Store output persistently, not under `/tmp`.

**First handoff:** the Sucofindo team reviews snippet merging with its Compose file.
Run validation from the package root, using the actual team's Compose file path:

```bash
sudo bash scripts/compose -f <team-compose.yml> -f <output-dir>/application.override.json config -q
```

Adjust the top-level `version` if a legacy `docker-compose` binary requires it.
Automation does not run Compose, create networks, or change application networks.
The selected existing network must connect web/API/MySQL and the Agent.
Resolve `mysql_host` through the database network alias, not localhost.

## 3. Agent and SSI during maintenance

```bash
bash scripts/datadog-bootstrap agent-start --dry-run
bash scripts/datadog-bootstrap agent-start
sudo bash scripts/datadog-bootstrap ssi-install --dry-run
sudo bash scripts/datadog-bootstrap ssi-install --maintenance
```

Run sudo from the package directory or pass absolute `--config`/`--secrets` paths.
The Agent uses `unless-stopped`, the existing network, APM/DogStatsD sockets,
host/container metrics, and log collection. No public host ports are published.
PHP sends traces to `unix:///var/run/datadog/apm.socket`; RUM uses
`http://<agent_name>:8126` over the Docker network. Bind the host socket directory
into applications and verify access as the Apache/PHP-FPM worker, not only root/CLI.

Existing Agents are not deleted/replaced. A package-managed Agent with the same
fingerprint is only health-checked; changed specifications or other Agents require
team review. API key rotation changes the fingerprint and is not applied silently.
Discovery uses Agent names/images and the host service; operators must audit custom
images without recognizable markers.

Existing SSI with `dd-shim` and the PHP package is detected and skipped. Partial
installations stop the stage. daemon.json/preload are backed up before installation.
The host installer can change Docker configuration/runtime, so an approved maintenance
window is required. After a timeout, inspect installer processes and Docker state
before retrying; a CLI timeout is not transactional rollback.

**Second handoff:** once SSI is ready, the team applies environment/socket mounts
and recreates web/API containers. A new MySQL startup configuration mount requires
recreating MySQL with the same data volume. If the mount already exists and only its
configuration content changes, restarting MySQL is sufficient. New environment,
image, or mount definitions require recreation. The package performs none of these lifecycle actions.

## 4. DBM SQL: choose one path

Operator path with local administrator credentials:

```bash
bash scripts/datadog-bootstrap dbm-apply --dry-run
bash scripts/datadog-bootstrap dbm-apply --maintenance
```

Read checks precede SQL mutation: server version/datadir/application schemas,
existing monitoring account/login and connection limit, and procedure definitions,
signatures, and security. Existing passwords are not changed; different or unreadable
procedures are not overwritten. New accounts have a five-connection limit.
Base grants include PROCESS, REPLICATION CLIENT, SELECT on performance_schema and
index statistics, and procedure EXECUTE. Application data SELECT is not granted.
Schema metadata collection/REFERENCES is not enabled by default; this optional DBM
feature can be added through a separate change.

SQL literals use explicit session `NO_BACKSLASH_ESCAPES` and doubled quotes;
identifiers are strictly validated. Client passwords travel through stdin, not argv.
Credentials briefly reside in the MySQL client process environment inside the
container and remain visible to root. SQL error output is withheld to prevent leaks.
Procedures use `SQL SECURITY DEFINER`; the DBA reviews the definer and privileges.
Do not remove the administrator/definer after provisioning. DDL is not transactional;
inspect partial failures before rerunning.

DBA path without sharing administrator credentials with the operator:

```bash
bash scripts/datadog-bootstrap dbm-export
```

Transfer `dbm-dba-review.sql` securely. The DBA reads the initial version/datadir
SELECT results and inspects schemas, existing users/passwords/grants, and
`SHOW CREATE PROCEDURE`. Omit CREATE blocks only for identical existing procedures;
stop for review if definitions differ. Run the client **without `--force`**.
This is a review template, not a blind rerunnable migration; it contains no DROP or
password rotation. After DBA completion, use monitoring credentials for verification.
Startup configuration and SQL are separate stages; SQL provisioning never restarts MySQL.

## 5. RUM and persistence

Use production-specific application ID, public client token, and remote configuration
ID. [Apache auto-injection][rum] was preview at the documentation review; check
site/account availability and egress before maintenance. The Agent must be reachable from web.

```bash
bash scripts/datadog-bootstrap rum-install --dry-run
bash scripts/datadog-bootstrap rum-install --maintenance
```

The script backs up the Apache root, checks configurator flags, runs the installer,
adds `DatadogTracing Off` so Apache handles RUM while PHP uses SSI, tests configuration,
and reloads gracefully. Backups include a container manifest. Installer/configuration
test failure returns nonzero; rollback is not automatic. Review downstream installer
reload behavior first—the wrapper controls only its own reload. Existing modules
are skipped to avoid duplicate installation; the team verifies they use production RUM settings.

The `rum-persistence-*` output exports Apache configuration and `/opt/datadog-httpd`,
with a **RUM-only** Dockerfile example and alternative bind-mount snippet.
Review LoadModule/Include entries, external assets/library dependencies, symlinks,
ownership, base image, and Apache ABI. Do not blindly publish full production
configuration in an image; exports may contain sensitive virtual host/TLS settings.

The recommended approach is to cherry-pick RUM changes into the team's web image
using the same base, restore the original USER, and build/test in staging. Alternatively,
host mounts preserve the entire Apache configuration and require managing it across
future deployments. Do not use both approaches simultaneously.

**Third handoff:** the team builds/deploys a persistent RUM image or applies reviewed
mounts, recreates web containers, and verifies again. Initial `docker exec` installation
does not establish persistence. If export fails after installation, use `rum-export`
to resume without reinstalling.

## 6. Validation and permitted conclusions

```bash
bash scripts/datadog-bootstrap verify
bash scripts/datadog-bootstrap smoke --dry-run
bash scripts/datadog-bootstrap smoke
```

Verification checks Agent health, SSI runtime, CLI tracer, socket access, allowlisted
environment settings, Performance Schema, consumers, executable explain procedures,
MySQL check JSON, Apache module, and the Agent endpoint. Critical failures return exit 1.
Smoke testing sends GET requests to operator-selected endpoints: use a read-only
endpoint that actually executes SQL. Response bodies are not printed. SDK markers
in HTML prove injection only.

Verification marks only performed local checks as PASS. Review optional status
sections using the [checklist](VALIDATION.md). A CLI extension does not prove HTTP
SAPI instrumentation or delivered telemetry. Generate browser sessions, API requests,
and SQL, then verify RUM–APM and APM–DBM links in Datadog. Configure Allowed Tracing
URLs, sampling, propagators, CSP, and CORS for the application; a loaded RUM module
does not establish correlation.

Every stage supports `--dry-run`. It does not write files, download installers,
mutate SQL, install modules, reload/recreate containers, or start containers.
It still requires valid host/configuration and performs read-only discovery/probes.
DBM apply dry-run requires administrator credentials for read checks.
There is no automatic remote deployment or SSH execution.

## Agent features versus the development baseline

| Baseline | Production implementation |
| --- | --- |
| Infrastructure/APM, non-local DogStatsD, sockets | Enabled; parameterized network/socket, no published host ports |
| All container logs and automatic multiline detection | Enabled; excludes the Agent using its configured name |
| Process collection | Enabled by default; requires host PID and read-only passwd/group |
| Runtime security | Disabled by default; independent opt-in |
| Network monitoring | Disabled by default; independent opt-in |
| Universal Service Monitoring | Disabled by default; independent opt-in |
| Host root/os-release | Mounted when runtime security or USM is selected |
| debugfs, host cgroup, unconfined AppArmor | Only when a system-probe feature is selected |
| SYS_ADMIN, SYS_RESOURCE, SYS_PTRACE, NET_ADMIN, NET_BROADCAST, NET_RAW, IPC_LOCK, CHOWN | Added for system-probe options following official examples; broad host access |
| KILL | Omitted: automated response is out of scope and official monitoring examples do not require it |
| Agent run directory, Docker socket/proc/cgroup/container logs | Preserved with parameterized host paths |

The three opt-in features are not automatically enabled in production. Review
requirements, kernel/eBPF support, billing, privileges, and health before enabling
each. The package does not use `--privileged`. See [CNM][cnm], [USM][usm], and
[Workload Protection][security] for capabilities/platform requirements. If runtime
compilation requires kernel headers, the team adds appropriate mounts following
official documentation; the package does not install kernel packages or automatically weaken host policy.

## Local testing and rollback

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Tests cover validation, secret encoding, label/service mapping, dry-run, render
reruns, SQL conflicts/existing objects, checksums, Agent/SSI guards, timeouts, and
failure redaction. Real Docker/credentials are not required. Mocks do not replace
integration tests on Ubuntu/Apache/MySQL or production traffic. Complete acceptance
in equivalent staging before production maintenance.

See [component rollback](ROLLBACK.md); the package never deletes database data/volumes.
[Local test results](LOCAL-TESTS.md) record workspace verification and its limitations.

## Official references

- [Docker SSI][ssi] and [SSI compatibility][compat].
- [PHP compatibility][php] and [APM–DBM correlation][correlation].
- [Self-hosted MySQL DBM][dbm].
- [Apache RUM][rum], [RUM installer][ruminstaller], and [RUM–APM][rumapm].
- [CNM][cnm], [USM][usm], and [Workload Protection for Docker][security].
- [MySQL string literals][mysqlstrings], [CREATE PROCEDURE][mysqlproc],
  [accounts/grants][mysqlusers], and [Performance Schema startup][mysqlps].

[ssi]: https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/docker/
[compat]: https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/compatibility/
[php]: https://docs.datadoghq.com/tracing/trace_collection/compatibility/php/
[correlation]: https://docs.datadoghq.com/database_monitoring/connect_dbm_and_apm/
[dbm]: https://docs.datadoghq.com/database_monitoring/setup_mysql/selfhosted/
[rum]: https://docs.datadoghq.com/real_user_monitoring/application_monitoring/browser/setup/server/apache/
[ruminstaller]: https://rum-auto-instrumentation.s3.amazonaws.com/installer/latest/install-proxy-datadog.sh
[rumapm]: https://docs.datadoghq.com/tracing/other_telemetry/rum/
[cnm]: https://docs.datadoghq.com/network_monitoring/cloud_network_monitoring/setup/
[usm]: https://docs.datadoghq.com/universal_service_monitoring/setup/
[security]: https://docs.datadoghq.com/security/workload_protection/setup/docker/
[mysqlstrings]: https://dev.mysql.com/doc/refman/8.0/en/string-literals.html
[mysqlproc]: https://dev.mysql.com/doc/refman/8.0/en/create-procedure.html
[mysqlusers]: https://dev.mysql.com/doc/mysql-security-excerpt/8.0/en/creating-accounts.html
[mysqlps]: https://dev.mysql.com/doc/refman/8.0/en/performance-schema-quick-start.html
