# Datadog Deployment — Sucofindo

Staged automation for production Ubuntu and Docker: Infrastructure Monitoring,
container logs, host-based SSI for APM, MySQL DBM, and Apache RUM auto-injection.

The staged `datadog-bootstrap` command prepares configuration and handoffs without
editing the main Compose file or recreating applications. For the supplied
`/opt/eminerba_docker` stack, the opt-in [production coordinator](docs/EMINERBA-PRODUCTION.md)
performs the full lifecycle with one command after initial configuration:

```bash
sudo bash scripts/eminerba-production --maintenance
```

This command recreates web/API/database containers during maintenance and preserves
their existing mounts. Read its setup and validation requirements before use.

## Prerequisites

- Ubuntu host, local rootful Docker, Bash, Python ≥3.8, and administrator access.
- Apache/PHP 8.1 web/API containers and MySQL on a network accessible to the Agent.
- A new API key, production RUM settings, database backups, and a maintenance window.

Preflight checks versions, additional tools, and compatibility.
See the [full prerequisites](docs/DEPLOYMENT.md#package-and-prerequisites).

## Getting started

Testing without the original application images? Start with the
[production-layout rehearsal](lab/rehearsal/README.md) to test the same coordinator
with dummy PHP/MySQL applications and `env:lab`:

```bash
sudo bash scripts/eminerba-lab prepare
# Fill lab Datadog configuration and reviewed installer hashes once.
sudo bash scripts/eminerba-lab --maintenance
```

The original [standalone staged lab](lab/README.md) remains available for manual
stage-by-stage testing. Use separate clean VMs for these two fixtures.

Run from the package directory on the target host:

```bash
cp config/production.example.json config/production.json
cp config/secrets.example.json config/secrets.json
chmod 600 config/secrets.json
bash scripts/datadog-bootstrap discover
```

Fill in the [production configuration](config/production.example.json) using discovery
results and populate the [local secrets](config/secrets.example.json). Replace every
placeholder; never commit credentials. Container names may differ from Compose service names.

```bash
bash scripts/datadog-bootstrap preflight
bash scripts/datadog-bootstrap render --dry-run
```

Discovery and preflight do not modify the server. Every stage supports
`--dry-run`, which may still perform discovery and read-only probes.

## Deployment workflow

Follow the [deployment runbook](docs/DEPLOYMENT.md) for commands and stage requirements.
Do not execute all stages as an unattended batch without review.

| Stage | Output / team action |
| --- | --- |
| Preparation | Discovery, preflight, installer download and checksum review |
| Render | Review Agent, MySQL, and application configuration |
| Agent + SSI | Install; the team applies snippets and recreates application/database containers as needed |
| DBM | Provision SQL through the operator or a DBA-reviewed SQL file |
| RUM | Install, test configuration, and hand off persistent image/mount changes |
| Validation | Retest after recreation; prove telemetry and correlation in Datadog |

Use `sudo bash scripts/compose ...` for automatic selection of `docker compose`
or legacy `docker-compose`. The lab's `dc` helper uses this wrapper.

## Safety boundaries

- SSI runs on the host; the package does not install the PHP tracer manually.
- Existing Agents, DBM passwords, and conflicting procedures are not silently replaced.
- Runtime security, network monitoring, and USM are separate options requiring privilege review.
- RUM installed through `exec` is not persistent until the image/mount handoff is complete.
- Local verification does not prove end-to-end telemetry or production deployment success.

## Repository layout

| Path | Purpose |
| --- | --- |
| `scripts/` | Production automation, Compose helper, and stable command entry points |
| `config/` | Configuration examples and ignored local configuration/secrets |
| `datadog/` | Datadog configuration and SQL templates |
| `lab/rehearsal/` | Production-layout lab implementation, Docker templates, and guide |
| `lab/` | Original staged lab and dummy PHP/MySQL assets shared with rehearsal |
| `docs/` | Production runbooks, validation, rollback, and test results |
| `tests/` | Offline checks and optional local tool integration tests |

Existing lab configuration paths and generated `/opt/eminerba-rehearsal/` paths
remain unchanged. Use `scripts/eminerba-lab` as the rehearsal entry point.

## Documentation

- [Deployment runbook, technical details, and official references](docs/DEPLOYMENT.md)
- [Eminerba production coordinator and one-time setup](docs/EMINERBA-PRODUCTION.md)
- [Validation and sign-off checklist](docs/VALIDATION.md)
- [Component-level rollback](docs/ROLLBACK.md)
- [Local test results and limitations](docs/LOCAL-TESTS.md)

## Testing

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Tests use mocks without real servers or credentials. Acceptance testing in a
production-equivalent staging environment is still required before maintenance.
