# Eminerba offboarding

Offboarding restores the recorded application images and mounts, removes the
deployment's Agent container, and removes owned DBM SQL objects and host SSI.
Application databases, volumes, source files and uploads are preserved.
This requires maintenance: applications and MySQL are recreated, and removing
host SSI restarts Docker. Run from the deployment repository on the target host.

```bash
sudo bash scripts/eminerba-production offboard --dry-run
sudo bash scripts/eminerba-production offboard --maintenance
```

The generated lab uses the same implementation on its dedicated VM:

```bash
sudo bash scripts/eminerba-lab offboard --dry-run
sudo bash scripts/eminerba-lab offboard --maintenance
```

## Requirements and ownership

Keep the matching configuration/secrets, `recovery.json`, original images,
unchanged Compose/.env and existing data mounts. MySQL administrator credentials
must still work. Offboarding does not require a healthy Agent, valid Datadog API
key, installer downloads or installer checksums.

Starting with this version, SSI installation and DBM provisioning record
`offboarding-ownership.json` in the configured output directory before changes.
It contains resource identities, procedure definitions and account fingerprints,
not passwords. Reruns preserve original ownership. Configuration drift and
incomplete installation records stop automatic component cleanup.

Older deployments without these records can still restore applications and remove
the matching managed Agent. Unproven SSI and SQL resources are retained and reported
for manual cleanup. Pulling an update does not retroactively prove ownership.
Do not fabricate records or reapply a broken deployment to generate them; follow
the manual component procedures in [ROLLBACK.md](ROLLBACK.md).

## Workflow

1. Validate the recovery baseline, Agent identity and ownership inventory. Check
   SQL fingerprints while MySQL is running; a stopped database can be recovered
   first. Dry-run performs read-only checks and writes no files.
2. Run application rollback with original image IDs, `runc`, original mounts and
   disabled tracing. Additional RUM, MySQL config and socket mounts are removed.
3. Stop and remove only the matching managed Agent, rechecking its immutable ID.
   No Docker volumes are deleted.
4. Drop only recorded newly created DBM procedures with matching bodies,
   signatures and definers. Other routine grants block deletion. Drop a created
   monitoring account only if authentication/grant fingerprints match and it is
   not the definer of other objects. Pre-existing accounts/grants remain for DBA
   review. Drop a created `datadog` schema only if empty; never drop application
   schemas, tables or data.
5. Use the installed `datadog-installer` to uninstrument Docker and remove owned
   injector/PHP packages. Restart Docker, require its default runtime to be `runc`,
   and start/check the recovered services. Other running containers, stopped
   containers using `dd-shim` or Datadog sockets, host process injection and other
   language libraries block this host-wide operation. Partial installs and
   unsupported installer layouts require manual cleanup. Package directories
   are never removed with filesystem deletion.
6. Verify original images/mounts/runtime, Apache config, absence of the Datadog
   Apache module, disabled tracing and optional HTTP readiness. Write
   `offboarding-report.json` and update `deployment-status.json`.

Datadog documents SSI removal and the required Docker restart in its
[Docker SSI removal guide](https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/docker/).
The installed `apm uninstrument docker` and package `remove` commands are checked
before cleanup. See the official [command source](https://github.com/DataDog/datadog-agent/blob/main/pkg/fleet/installer/commands/apm.go)
and [injector removal hook](https://github.com/DataDog/datadog-agent/blob/main/pkg/fleet/installer/packages/apm_inject_linux.go).

## Shared components and results

Retain shared host instrumentation or monitoring SQL explicitly:

```bash
sudo bash scripts/eminerba-production offboard --dry-run --keep-ssi --keep-dbm
sudo bash scripts/eminerba-production offboard --maintenance --keep-ssi --keep-dbm
```

These options skip only the specified cleanup. Application rollback, Agent removal
and application checks still run. `--keep-ssi` avoids the Docker restart; container
recreation still requires maintenance.

| Exit | Result |
| --- | --- |
| `0` | Dry-run checks succeeded, or recorded component cleanup completed |
| `1` | A check/operation failed; inspect the failed stage before retrying |
| `2` | Applications recovered, but retained components require review; see the report |

Keep options, legacy ownership, pre-existing DBM accounts and occupied created
monitoring schemas are listed as retained. Failed checks stop execution without
undoing completed cleanup steps. Retries accept absent Agents and already removed
owned SQL objects. Use `rollback.override.json` for later uninstrumented Compose
operations, not `production.override.json`.

## Follow-up

Test login and business requests, confirm browser HTML has no RUM injection, and
verify new telemetry stops arriving. Readiness probes do not prove business-flow
correctness. Local secrets, installer files, recovery images and backups remain
for audit/recovery. Datadog keys/tokens, application settings and previously
ingested telemetry are not changed remotely. Revoke unused credentials separately
after confirming they are not shared, and manage local secret retention separately.
