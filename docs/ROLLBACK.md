# Application recovery and component rollback

## Eminerba application rollback command

Deployment stops on failure. **Rollback is explicit, not automatic.** A failed
installer can leave changes or downstream processes behind; inspect the failed
stage before starting recovery. Never run deployment, standalone stages, Docker
maintenance, and rollback concurrently. The coordinator lock serializes its own
apply/rollback and rehearsal commands, not external Docker commands or standalone
`datadog-bootstrap` stages.

For production, from the repository:

```bash
sudo bash scripts/eminerba-production rollback --dry-run
sudo bash scripts/eminerba-production rollback --maintenance
```

For the generated lab:

```bash
sudo bash scripts/eminerba-lab rollback --dry-run
sudo bash scripts/eminerba-lab rollback --maintenance
```

Apply now records a private `recovery.json` before container/host changes. It
contains the base Compose model hash, project, original image IDs and mount
mapping, not a database backup or resolved credentials. It is retained on reruns.
Original images receive local recovery tags; do not prune them. Older deployments
without this file require the manual procedure below. A subsequent healthy apply
can create it using the original image aliases in existing production state.
Do not run a broken deployment merely to manufacture a recovery file.

The rollback command works with stopped or missing application containers and
does not require Agent health, installer downloads, or working PHP preflight. It
checks local rootful Docker, unchanged Compose/.env, image availability, container
ownership, existing bind paths and named volumes before recreating anything.
Missing volumes are an error; recovery must not initialize an empty replacement.

It recreates DB first using the original image and data mount, waits for SQL,
verifies image/runtime/mounts, then recreates API and web. All use `runc` with
`DD_INSTRUMENT_SERVICE_WITH_APM=false` and `DD_TRACE_ENABLED=false`. Original base
mounts remain; the coordinator's extra MySQL, socket, and RUM mounts are omitted.
Apache configuration tests run on API/web. There is no image build or database
restore. MySQL is restarted, so this is a maintenance operation with downtime.

After completion, verify login, a read-only business request, API/database access,
and application logs. Container readiness and Apache syntax are not business
acceptance. For subsequent uninstrumented Compose operations, use the same project,
base Compose and generated `rollback.override.json`; do not also include
`production.override.json`. Applying observability again uses the apply command
after the failure cause has been fixed and the dry-run passes.

**What application rollback does not reverse:**

- Database contents, DBM users/grants/procedures and runtime consumer changes.
- Host SSI packages, Docker default runtime, Agent or shared socket directory.
- Changes to bind-mounted source files, `.env`, `my.cnf`, or uploaded application data.
- Unrecorded edits inside a container's writable layer; original images do not
  contain these. Export required changes before any apply/recreation.
- Datadog-side settings or telemetry already ingested.

Use the component procedures below for these. Never restore a stale database
backup automatically: that can discard transactions written after deployment.
If Docker cannot start, recover the host runtime first. If the base Compose/.env
has changed, manually reconcile it with the recorded baseline before recovery;
do not edit hashes to bypass checks.

## Failure decision guide

| Failed stage | Next action |
| --- | --- |
| Preflight | Correct configuration or missing schema; no deployment changes have started |
| Recovery snapshot/render/image preparation | Inspect artifacts/build failure; application recreation has not started |
| Agent / SSI | Inspect Agent and host runtime; resolve a partial installer before retry or host rollback |
| DB recreation / readiness | Inspect DB logs locally; use application rollback if Docker and original data/image are available |
| DBM SQL | Inspect partial grants/procedures; rollback containers does not undo SQL |
| API/web or RUM | Use application rollback to remove added mounts and restore original images; inspect host installer processes first |
| Verify / smoke | Diagnose application and observability separately; successful container start is not telemetry acceptance |
| Rollback itself | Stop and inspect the reported stage; no automatic rollback of rollback |

`deployment-status.json` records the last stage and status without command output
or credentials. Ctrl-C and handled errors record interruption/failure. Power loss
or SIGKILL can leave `running`; this means the final outcome is unknown, not success.

## Manual component procedures

The Sucofindo team performs rollback during an approved maintenance window.
No subcommand deletes database volumes or data. Record configuration, image, and
container IDs before and after changes. MySQL DDL and host installers are not transactional.

## Agent

1. Confirm that the selected container is the Agent introduced by this change, not
   an existing Agent. Match its name, image, inventory, and
   `id.sucofindo.datadog.managed` label.
2. Stop the new Agent if necessary: `docker stop <new-agent>`. Applications and
   databases remain unchanged; telemetry stops. Preserve the container/configuration for investigation.
3. If the team previously replaced an Agent manually, restore its former definition,
   image, and configuration. Start it only after stopping the new Agent; do not run two Agents.
4. To disable only runtime security/CNM/USM, change the flags and review the new
   specification. The team recreates **only the Agent**; the package refuses automatic specification changes.
5. If applications remain instrumented while the Agent is stopped, assess tracer
   buffering/errors and decide whether to disable instrumentation as part of application rollback.

Revoke the new API key under the team's credential procedure once it is no longer
used. Generated MySQL configuration and local secrets remain sensitive; manage
their retention. Do not delete `/var/run/datadog` while other applications or Agents use its sockets.

## Host SSI and PHP

1. To stop tracing selected services, the team sets
   `DD_INSTRUMENT_SERVICE_WITH_APM=false` and `DD_TRACE_ENABLED=false` in the
   deployment, then **recreates** application containers to apply the new environment.
2. For a full host SSI rollback, follow the official uninstall procedure for the
   installed version. Docker documentation lists `dd-container-install --uninstall`
   followed by a Docker restart. Confirm the binary is available and review
   `--help`; modern installer layouts may use `/opt/datadog-packages/run/`.
3. Restarting Docker affects other containers. The team schedules and performs it,
   not this script. Preserve the `ssi-before-*` backup (daemon.json/preload).
   Do not blindly overwrite daemon.json if other Docker changes were made after the backup.
4. Verify that the default runtime is restored and configuration does not reference
   a removed runtime. The team recreates applications to remove residual injection.
5. Verify normal PHP HTTP operation, no duplicate ddtrace, and normal error rates/latency.

After an installer timeout, downstream subprocesses may still be running. Inspect
processes, package state, and the Docker runtime before uninstalling or retrying.
Do not run installation and uninstallation concurrently.

## Applications and MySQL startup configuration

1. The team restores service/version/DBM propagation environment variables, socket
   mounts, and images to the previous deployment revision. Environment/image/mount changes require recreation.
2. Restore the pre-Datadog MySQL configuration or remove the additional mount through
   the team's Compose deployment. Changing mounts requires MySQL recreation; with
   unchanged mounts, a restart is sufficient for startup-only variables.
3. **Preserve exactly the same database volume/data directory.** Do not use
   `down -v`, prune volumes, remove the data directory, or initialize a new database.
4. Verify version/datadir, readiness, and application operation after restarting.
5. Runtime consumers may remain enabled after provisioning. The DBA restores their
   prior values if necessary; do not disable all consumers without an audit.

## DBM SQL objects that remain

Stopping the Agent or restoring startup configuration does not remove the monitoring
account, grants, `datadog` schema, `datadog.explain_statement`, runtime consumer
procedure, or `<application-schema>.explain_statement`. These remain until the DBA
takes separate action. The script does not rotate existing passwords.

The DBA compares before/after inventories and confirms no other monitoring uses
these objects. After reviewing definers and dependencies, the DBA may revoke grants
or drop objects **created by this change only** if no longer needed. Do not broadly
delete application schemas or a pre-existing datadog schema. After partial
provisioning failures, inspect account/routine state; reruns verify existing objects
and stop on conflicting definitions.

## Apache RUM

1. Select the `rum-before-*` backup with the correct container ID. If configuration
   testing fails, do not reload until it is fixed. The vendor installer may already
   have changed configuration; the wrapper does not guarantee transactional rollback.
2. Compare the Apache root with the backup, restore only modified files, then
   disable/remove Include/LoadModule entries or enabling symlinks **added by the installer**.
   Copying a backup over an existing directory does not remove newly created files.
3. Preserve original virtual hosts, TLS, authentication, and routing. Run Apache
   `-t`; reload gracefully only if it passes. Check normal HTML requests without injection.
4. If RUM was made persistent, revert the RUM image to its previous revision or remove
   RUM mounts through the team's deployment, then recreate web containers. Do not
   remove assets still referenced by other containers.
5. PHP SSI/APM may remain active when rolling back only RUM. There is no manually
   installed PHP tracer to remove because this package does not install one.
6. Restore production RUM settings (tracing URLs/sampling) only if they were part
   of this change; preserve previous settings. Do not substitute a development application ID.
