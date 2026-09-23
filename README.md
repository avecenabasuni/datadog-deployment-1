# Datadog Deployment — Sucofindo

Staged automation for production Ubuntu and Docker: Infrastructure Monitoring,
container logs, host-based SSI for APM, MySQL DBM, and Apache RUM auto-injection.

The package prepares configuration and handoffs. It does not edit the main Compose
file; the Sucofindo team remains responsible for application and database restarts
or container recreation.

## Prerequisites

- Ubuntu host, local rootful Docker, Bash, Python ≥3.8, and administrator access.
- Apache/PHP 8.1 web/API containers and MySQL on a network accessible to the Agent.
- A new API key, production RUM settings, database backups, and a maintenance window.

Preflight checks versions, additional tools, and compatibility.
See the [full prerequisites](docs/DEPLOYMENT.md#package-and-prerequisites).

## Getting started

Testing without the original application images? Start with the
[standalone Ubuntu lab](lab/README.md): two PHP 8.1 services, MySQL 8.0, and `env:lab`.

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

Compose examples use `docker-compose`.

## Safety boundaries

- SSI runs on the host; the package does not install the PHP tracer manually.
- Existing Agents, DBM passwords, and conflicting procedures are not silently replaced.
- Runtime security, network monitoring, and USM are separate options requiring privilege review.
- RUM installed through `exec` is not persistent until the image/mount handoff is complete.
- Local verification does not prove end-to-end telemetry or production deployment success.

## Documentation

- [Deployment runbook, technical details, and official references](docs/DEPLOYMENT.md)
- [Validation and sign-off checklist](docs/VALIDATION.md)
- [Component-level rollback](docs/ROLLBACK.md)
- [Local test results and limitations](docs/LOCAL-TESTS.md)

## Testing

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Tests use mocks without real servers or credentials. Acceptance testing in a
production-equivalent staging environment is still required before maintenance.
