# Eminerba production: one deployment command

This opt-in coordinator targets the supplied three-service stack in
`/opt/eminerba_docker/docker-compose.yml`. It **does recreate production
containers** when invoked with `--maintenance`. It is separate from the original
staged `datadog-bootstrap` command, which still leaves recreation to operators.
Run during a maintenance window with an available database backup/restore path.
SSI affects the host Docker runtime, including other workloads on that host.

The coordinator has offline regression tests. It has not been run against the
production machine; exercise the same layout in staging before production use.
The [production-layout lab](../lab/rehearsal/README.md) creates dummy applications
and uses this same coordinator with an explicitly isolated `env:lab` profile.

## Existing stack mapping

| Role | Compose service | Container | Existing mounts retained |
| --- | --- | --- | --- |
| Web | `web` | `eminerba_web` | `pit_to_port:/var/www/html`, PHP session volume |
| API | `api` | `eminerba_api` | `apiminerba:/var/www/html` |
| MySQL | `db` | `eminerba_db` | actual `mysql_data` volume, `docker/my.cnf` |

The actual project name and shared network come from running containers, not the
directory name. All three must already be running under the supplied Compose file.
Additional services, unrecognized mounts/Compose overlays, or an unrelated Agent
stop this workflow for review. The command does not adopt an existing unmanaged
Agent or RUM module. A `ddagent-install.log` alone does not prove Agent ownership.

The supplied images run Apache/PHP 8.1 as root initially. Both PHP database
extensions are installed, but the actual CodeIgniter driver still needs checking.
The coordinator does not edit application/database credentials or PHP source.

## One-time configuration

Run from this repository on the Ubuntu host, using Python 3.8+, Bash, Docker,
curl, tar, coreutils and systemd. `docker compose` is preferred. For legacy
`docker-compose` 1.29, install `python3-yaml` so the resolved Compose model can be
validated without printing environment secrets.

```bash
sudo bash scripts/eminerba-production prepare
sudo nano config/eminerba.json
sudo nano config/eminerba-secrets.json
```

`prepare` discovers the live network and attempts to read schema names only from
the web container's `DB_NAME`/`DB_NAME2`. Confirm that these include the API's
actual schemas. It downloads installer scripts for review without executing them.
It refuses existing configuration rather than generating different passwords.

Fill these once:

- `agent_image`: an exact reviewed `registry.datadoghq.com/agent:7.X.Y`, >=7.76.1.
- `site`, application `version`, production RUM application/client token/remote
  configuration ID.
- `mysql_schemas`: actual application databases, not container or service names.
- `rum_url`: reachable web HTML URL; `apm_url`: a safe, read-only API endpoint that
  exercises the database. The supplied ports are web 81 and API 80, but public
  browser URLs can differ behind a proxy.
- Secrets: new `api_key` and the existing MySQL admin password. Keep the generated
  `db_password` for the new DBM account; if a monitoring account already exists,
  provide its matching credentials and review its grants.
- `ssi_sha256` and `rum_sha256`: reviewed installer hashes. Read the scripts and
  their downstream download behavior, then record the SHA-256 values printed by
  `prepare`. Entry script hashes do not pin all downloaded dependencies.

Files containing credentials are private and ignored by Git. Neither `.env` nor
the original production Compose/Dockerfile is edited. A failed download during
`prepare` can be retried before configuration is created. Once configuration
exists, download missing scripts with `datadog-bootstrap fetch-installers` using
the explicit `--config config/eminerba.json --secrets config/eminerba-secrets.json`.

## Preview, then deploy

```bash
sudo bash scripts/eminerba-production --dry-run
```

This performs read-only inventory, Compose model, mount, database, credential,
and installer hash checks. Docker builds and downstream installer compatibility
cannot be proven by this preview. The lock file prevents concurrent coordinator runs.

The deployment itself is one command:

```bash
sudo bash scripts/eminerba-production --maintenance
```

It executes these steps and stops at the first failure:

1. Validate the actual project, database datadir, mounted volume, source binds,
   shared network, Agent ownership and reviewed installer hashes. Compare base
   Compose environment values with the live containers using in-memory hashes;
   changed credentials/environment stop deployment without printing their values.
2. Render settings. Tag the exact running image IDs locally. Build derived web/API
   images with missing curl/tar/gzip/GPG packages as needed; do not rebuild the
   application or pull a new PHP/MySQL base image.
3. Start the managed Agent and install/reuse host SSI.
4. Recreate only `db` with the same data volume, wait for SQL readiness, check
   startup settings, and provision DBM. The additional non-secret config is mounted
   read-only at `/etc/mysql/conf.d/zz-datadog.cnf` with mode 0644. Existing `my.cnf`
   stays mounted; effective settings are checked rather than assumed.
5. Recreate `api` and `web` with tracing environment/socket mounts.
6. Install web RUM, export Apache/module assets to the private output directory,
   attach the exports as read-only mounts, and recreate web to test persistence.
7. Run local verification and HTTP smoke tests after recreation.

All recreation uses the discovered project and `--no-deps --no-build`. Existing
mounts are checked again against the merged override before each recreation.
The original Compose/.env model must not change during the run. The command never
uses `down`, volume deletion, pruning, a MySQL version upgrade, or a PHP driver switch.

Rerunning the apply command is another maintenance operation: it can recreate the
three containers again. It retains passwords and includes existing managed RUM
persistence. Changed Compose/.env or RUM settings stop for review, rather than
silently treating an application upgrade as an instrumentation rerun.

## Subsequent deployments and failures

The generated `production.override.json` is required for subsequent application
operations. Running base Compose alone can remove instrumentation. The coordinator
prints the override path; default:

```text
/opt/eminerba-observability/generated/production.override.json
```

`production-state.json` records the original pinned image aliases and persistence
path. Keep the exported Apache/module directories on disk. They may contain
configuration secrets and must not be published. Future Apache/vhost changes must
be reconciled with these exported mounts; an app image rebuild alone does not
update the mounted Apache configuration.

On failure, the message names the stage. There is no automatic rollback of SQL,
SSI or Apache changes. Inspect the failed stage before retrying; in particular,
an interrupted RUM installation with no saved persistence state requires manual
review/export before adoption. Do not delete state or rotate credentials simply
to bypass a refusal. Use the [component rollback runbook](ROLLBACK.md) and recorded
image aliases; preserve the same database volume.

Local success is not end-to-end acceptance. Use the [validation checklist](VALIDATION.md)
to prove actual HTTP PHP tracing, browser RUM sessions, and SQL correlation in
Datadog. Configure Allowed Tracing URLs for the browser-visible API origin and
check CORS if web/API use different origins or ports. MySQLi availability does
not establish PDO-style APM–DBM correlation. External Apache Include/LoadModule
dependencies outside the exported roots may require a tailored persistence image.

The checks use Docker's [resolved Compose model](https://docs.docker.com/reference/cli/docker/compose/config/)
and scoped [Compose recreation options](https://docs.docker.com/reference/cli/docker/compose/up/).
