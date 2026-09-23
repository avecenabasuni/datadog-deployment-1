# Component-level rollback

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
