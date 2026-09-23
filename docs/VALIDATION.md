# Sucofindo acceptance checklist

Record the maintenance window, operator, application release, Agent image digest,
SSI/PHP versions, and change number. Do not include environment dumps, passwords,
or user request bodies. Status remains unaccepted until runtime and telemetry evidence is complete.

## Before making changes

- [ ] Ubuntu/kernel/architecture/Docker inventory matches the official compatibility matrix.
- [ ] The team approves web/API/database containers, Compose labels/project, and network.
- [ ] Audit the entire inventory for custom Agent images and host Agents; allow only one Agent per host.
- [ ] No manually installed PHP tracer or conflicting extension exists in images or web SAPI configuration.
- [ ] PHP CLI **and web SAPI** use 8.1; OPcache JIT/Xdebug/ionCube/NewRelic/Blackfire/pcov do not block SSI.
- [ ] MySQL `SELECT VERSION(), @@datadir` matches discovery; no unreviewed MariaDB/Percona distribution.
- [ ] A persistent volume/bind mount covers the actual datadir; database backups and the team's restore procedure are available.
- [ ] Confirm MySQL actually reads the target `99-datadog.cnf`; not every image reads `/etc/mysql/conf.d`.
- [ ] Identify the driver **used by CodeIgniter during requests** (PDO MySQL versus MySQLi), not merely installed extensions.
- [ ] Verify the new API key/site and production RUM application/client token/configuration ID.
- [ ] Review both installer entry scripts and transitive downloads; record hashes. RUM configurator help recognizes all required flags.
- [ ] Allow HTTPS/TLS egress to registries, installers, Datadog intake, and RUM CDN/intake without disabling TLS verification.
- [ ] Review security/CNM/USM options and host privileges separately; owners approve telemetry scope and billing.
- [ ] Review snippet merges with `docker-compose ... config -q`; do not print full configuration that may contain secrets.

## After the recreation handoff

- [ ] Docker's default runtime is `dd-shim`; **new** web/API containers use it.
- [ ] PHP tracer >=1.6 is present in the HTTP SAPI; CLI checks do not replace request tests.
- [ ] Web/API workers receive DD_ENV=prod, service/version, and DBM propagation settings.
- [ ] PHP-FPM `clear_env` or Apache environment handling does not discard instrumentation variables.
- [ ] `/var/run/datadog/apm.socket` exists on the host, Agent, and applications. Apache/FPM worker UIDs can connect; do not use chmod 777 without review.
- [ ] Web containers can reach `http://<agent_name>:8126/info`; the Agent can reach MySQL directly through its network alias.
- [ ] MySQL Performance Schema is ON, all three digest/text sizes are 4096, and current statements/waits plus history-long consumers are enabled.
- [ ] The DBA verifies accounts, grants, connection limits, and procedure definers. No unplanned password rotation occurred.
- [ ] The DBM user can execute explain procedures in datadog and application schemas.
- [ ] `verify` passes; `agent check mysql --json` reports no errors. Loaded host/user/dbm settings match the target without disclosing passwords.

## Agent feature health (manual, not just section presence)

Run `docker exec <agent> agent status` locally. Redact output before sharing it.
Section names and JSON structure may differ between Agent versions.

- [ ] Collector/forwarder are healthy, the API key is accepted, and there are no intake errors.
- [ ] The APM Agent is running and receives traces after API/web requests.
- [ ] The Logs Agent is running, expected container inputs are active, and sent byte/log counts increase.
- [ ] When selected, the Process Agent is running and process/container inventory appears in Datadog.
- [ ] When CNM is selected, system-probe/network starts without eBPF/permission/kernel errors and connection data is visible.
- [ ] When USM is selected, service-monitoring is healthy and HTTP services are observed; this does not replace PHP SSI traces.
- [ ] When runtime security is selected, security-agent/runtime is healthy and workloads are visible. Run self-tests/alert tests only under the team's procedures.
- [ ] Compare CPU, memory, disk, and log volume with the pre-window baseline.

## RUM persistence and injection

- [ ] Apache configuration testing passes before graceful reload and the Datadog module is loaded.
- [ ] Apache uses DatadogTracing Off; PHP APM still comes from SSI.
- [ ] Installed RUM settings/ID/token/site/remoteConfigurationId belong to production.
- [ ] HTML responses contain injection without duplicating an existing manually embedded SDK.
- [ ] CSP permits required scripts/connections. For Apache proxies, upstream compression/TLS does not prevent body filtering.
- [ ] RUM configuration/module are included in the team's image or reviewed persistent mounts, including external LoadModule/Include/library dependencies.
- [ ] Recreate using the team's deployment, then repeat configuration, module, and HTTP injection checks; retain persistence evidence.
- [ ] Apache backups/exports do not publish credentials or TLS keys to public registries or source control.

## End-to-end telemetry (required; not automated with additional API credentials)

1. Open the production application in a browser and execute a read-only flow that
   calls the API and MySQL. Record timestamp, service, resource, and status without
   PII. Do not use write requests for smoke testing.
2. In Datadog, confirm host/container infrastructure, real web/API logs, and an HTTP
   APM trace with SQL child spans. A CLI trace alone does not satisfy acceptance.
3. For RUM–APM, configure Allowed Tracing URLs only for required internal origins/paths,
   agreed trace/session sampling, and propagators in the RUM UI. Confirm the browser
   sends trace headers and proxies preserve them. For cross-origin requests, CORS
   OPTIONS and Access-Control-Allow-Headers must cover the headers actually used:
   traceparent/tracestate and/or x-datadog-trace-id, x-datadog-parent-id,
   x-datadog-origin, x-datadog-sampling-priority, plus baggage if used.
   Do not allow every domain through a wildcard. Evidence: a RUM resource opens the matching backend trace.
4. For APM–DBM, confirm propagation support for the actual driver and trace context
   in queries. Find a DBM query sample linked to the same SQL span; sampling can
   omit an individual request. Use repeated read-only traffic within an agreed
   timeout, never an unbounded loop. For MySQLi-only applications, correlation
   remains PENDING until actual-version support and working evidence are established.
   Do not switch drivers automatically.
5. Verify explain plans for safe queries against real tables, not only SELECT 1.
6. Observe error rates, latency, and resource usage for the team's agreed interval
   (for example, 15 minutes). Follow component rollback for regressions or duplicate instrumentation.

A zero exit code from `verify` means only its documented local checks passed.
It does not mean end-to-end deployment is ACCEPTED. Failed manual checks block
sign-off even when the script exits successfully.
