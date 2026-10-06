"""Offboard the recorded deployment, preserving application data and unowned resources."""
import hashlib
from pathlib import Path
import re
import shutil

import datadog_deploy as d
from eminerba_production import MAPPING
from eminerba_recovery import rollback, verify_recovered


def agent_target(app):
    names = app.docker("ps", "-a", "--format", "{{.Names}}").splitlines()
    name = app.c["agent_name"]
    if name not in names:
        return None
    item = app.inspect(name)
    config = str(app.out / "mysql.d/conf.yaml")
    d.need(item["managed"] == "true" and re.fullmatch(r"[a-f0-9]{64}", item["spec"] or "") and
           item["image"] == app.c["agent_image"] and
           any(m["Destination"] == "/etc/datadog-agent/conf.d/mysql.d/conf.yaml" and
               m["Type"] == "bind" and m["Source"] == config for m in item["mounts"]),
           "The Agent does not match this deployment; offboarding will not remove it.")
    return item


def ssi_plan(app, saved, keep, retained):
    if keep:
        retained.append("Host SSI retained by --keep-ssi.")
        return None
    if not saved:
        packages = app.run(["dpkg-query", "-W", "-f=${binary:Package} ${Status}\n",
                            "datadog-apm-inject", "datadog-apm-library-php"], check=False)
        if app.docker("info", "--format", "{{.DefaultRuntime}}") == "dd-shim" or \
                "install ok installed" in packages or any(
                    (Path("/opt/datadog-packages") / name).exists()
                    for name in ("datadog-apm-inject", "datadog-apm-library-php")):
            retained.append("Host SSI has no ownership record; follow the manual SSI uninstall procedure.")
        return None
    if saved.get("status") == "removed":
        return None
    d.need(saved.get("status") == "installed" and saved.get("original_runtime") == "runc" and
           saved.get("packages") == ["datadog-apm-inject", "datadog-apm-library-php"],
           "SSI ownership is incomplete or its original runtime is unsupported; inspect it or use --keep-ssi.")
    allowed = {name for _, name in MAPPING.values()} | {app.c["agent_name"]}
    names = app.docker("ps", "-a", "--format", "{{.Names}}").splitlines()
    for name in names:
        if name not in allowed:
            item = app.inspect(name)
            d.need(not item["running"] and item["runtime"] != "dd-shim" and
                   not any(m["Destination"] == "/var/run/datadog" for m in item["mounts"]),
                   "Other containers may depend on SSI or a Docker restart; use --keep-ssi or review the host.")
    preload = Path("/etc/ld.so.preload")
    d.need(not preload.exists() or "datadog" not in preload.read_text().lower(),
           "Host process injection is also enabled; use --keep-ssi and review shared instrumentation.")
    other_libraries = [path for path in Path("/opt/datadog-packages").glob("datadog-apm-library-*")
                       if path.name != "datadog-apm-library-php"]
    d.need(not other_libraries, "Other SSI language packages exist; use --keep-ssi and review shared instrumentation.")
    installer = shutil.which("datadog-installer")
    d.need(installer, "datadog-installer is required to remove owned SSI packages; use --keep-ssi for manual uninstall.")
    app.run([installer, "apm", "uninstrument", "--help"])
    app.run([installer, "remove", "--help"])
    return installer


def dbm_plan(app, saved, keep, retained):
    if keep:
        retained.append("DBM objects retained by --keep-dbm.")
        return None
    if not saved:
        retained.append("DBM has no ownership record; the DBA must review any monitoring account, grants and procedures.")
        return None
    if saved.get("status") == "removed":
        if not saved.get("user_created"):
            retained.append("Pre-existing DBM account and its grants retained; the DBA must review added grants.")
        if saved.get("schema_retained"):
            retained.append("The created datadog schema contains other objects; retained for DBA review.")
        return None
    d.need(saved.get("status") == "installed" and type(saved.get("user_created")) is bool and
           type(saved.get("schema_created")) is bool and isinstance(saved.get("procedures"), list),
           "DBM ownership is incomplete; review partial provisioning or use --keep-dbm.")
    expected = app.procedures()
    seen = set()
    for routine in saved["procedures"]:
        key = (routine["schema"], routine["name"])
        ddl = expected.get(key)
        d.need(key not in seen and ddl and routine["body"] == d.normalize_sql(ddl[ddl.index("BEGIN"):]) and
               isinstance(routine.get("definer"), str) and "@" in routine["definer"] and
               routine["parameters"] == ("IN:text" if key[1] == "explain_statement" else ""),
               "Recorded DBM procedure differs; manual review required.")
        seen.add(key)
    if saved["user_created"]:
        d.need(all(re.fullmatch(r"[a-f0-9]{64}", saved.get(key, ""))
                   for key in ("account_fingerprint", "grants_fingerprint")),
               "DBM account ownership is incomplete; review it or use --keep-dbm.")
    else:
        retained.append("Pre-existing DBM account and its grants retained; the DBA must review added grants.")
    return saved


def dbm_cleanup(app, saved, retained, dry=False):
    c = app.c
    account = d.literal(c["db_user"]) + "@" + d.literal(c["db_user_host"])
    where = " WHERE user=" + d.literal(c["db_user"]) + " AND host=" + d.literal(c["db_user_host"]) + ";"
    user_exists = app.mysql("SELECT COUNT(*) FROM mysql.user" + where) == "1"
    if saved["user_created"] and user_exists:
        actual = app.mysql("SELECT CONCAT_WS('|',plugin,authentication_string,max_user_connections) FROM mysql.user" + where)
        grants = app.mysql("SHOW GRANTS FOR " + account + ";")
        d.need(hashlib.sha256(actual.encode()).hexdigest() == saved["account_fingerprint"] and
               hashlib.sha256(grants.encode()).hexdigest() == saved["grants_fingerprint"],
               "DBM account or grants changed since provisioning; review it or use --keep-dbm.")
        definer = d.literal(c["db_user"] + "@" + c["db_user_host"])
        for table in ("routines", "views", "triggers", "events"):
            d.need(app.mysql("SELECT COUNT(*) FROM information_schema." + table + " WHERE definer=" + definer + ";") == "0",
                   "Other SQL objects depend on the DBM account; use --keep-dbm and ask the DBA to review it.")
    drops = []
    for routine in saved["procedures"]:
        where_routine = (" WHERE routine_schema=" + d.literal(routine["schema"]) +
                         " AND routine_name=" + d.literal(routine["name"]) + " AND routine_type='PROCEDURE';")
        definition = app.mysql("SELECT CONCAT(security_type,'|',COALESCE(routine_definition,'')) "
                               "FROM information_schema.routines" + where_routine)
        if not definition:
            continue
        d.need(definition.startswith("DEFINER|") and
               d.normalize_sql(definition.split("|", 1)[1]) == routine["body"],
               "DBM procedure changed since provisioning; use --keep-dbm and ask the DBA to review it.")
        d.need(app.mysql("SELECT definer FROM information_schema.routines" + where_routine) == routine["definer"],
               "DBM procedure owner changed; use --keep-dbm and ask the DBA to review it.")
        params = app.mysql("SELECT CONCAT(parameter_mode,':',data_type) FROM information_schema.parameters "
                           "WHERE specific_schema=" + d.literal(routine["schema"]) + " AND specific_name=" +
                           d.literal(routine["name"]) + " ORDER BY ordinal_position;")
        d.need(params == routine["parameters"], "DBM procedure signature changed; manual review required.")
        other_grants = app.mysql("SELECT COUNT(*) FROM information_schema.routine_privileges WHERE routine_schema="
                                 + d.literal(routine["schema"]) + " AND routine_name=" + d.literal(routine["name"])
                                 + " AND grantee NOT IN (" + d.literal(account) + ","
                                 + "CONCAT(CHAR(39),SUBSTRING_INDEX(CURRENT_USER(),'@',1),CHAR(39),'@',CHAR(39),"
                                 + "SUBSTRING_INDEX(CURRENT_USER(),'@',-1),CHAR(39)));")
        d.need(other_grants == "0", "Other accounts use a DBM procedure; use --keep-dbm and ask the DBA to review it.")
        drops.append("DROP PROCEDURE IF EXISTS " + d.identifier(routine["schema"]) + "." + d.identifier(routine["name"]) + ";")
    if dry:
        return
    for sql in drops:
        app.mysql(sql)
    if saved["user_created"] and user_exists:
        app.mysql("DROP USER IF EXISTS " + account + ";")
    if saved["schema_created"]:
        occupied = sum(int(app.mysql("SELECT COUNT(*) FROM information_schema." + table + " WHERE " + column + "='datadog';"))
                       for table, column in (("tables", "table_schema"), ("routines", "routine_schema"), ("events", "event_schema")))
        if occupied:
            retained.append("The created datadog schema now contains other objects; retained for DBA review.")
            saved["schema_retained"] = True
        else:
            app.mysql("DROP SCHEMA IF EXISTS datadog;")
    saved["status"] = "removed"
    app.record_offboarding("dbm", saved)


def offboard(flow, dry=False, keep_ssi=False, keep_dbm=False):
    app = flow.app
    flow.step("validate offboarding plan and ownership")
    rollback(flow, dry=True)
    ownership = app.offboarding_manifest()
    retained = []
    agent = agent_target(app)
    installer = ssi_plan(app, ownership.get("ssi"), keep_ssi, retained)
    dbm = dbm_plan(app, ownership.get("dbm"), keep_dbm, retained)
    if dbm:
        app.secret("admin_password")
        # Dry-run can validate SQL while DB is running; rollback can recover a stopped DB first.
        names = app.docker("ps", "-a", "--format", "{{.Names}}").splitlines()
        if app.c["mysql_container"] in names and app.inspect(app.c["mysql_container"])["running"]:
            dbm_cleanup(app, dbm, retained, dry=True)
    steps = ["application rollback"]
    if agent:
        steps.append("remove owned Agent")
    if dbm:
        steps.append("clean owned DBM SQL")
    if installer:
        steps.append("remove owned SSI and restart Docker")
    d.log("PLAN: " + " -> ".join(steps) + ".")
    for reason in retained:
        d.log(reason, "WARN")
    if dry:
        d.log("Offboarding dry-run completed. No resources changed; API keys and local recovery files are retained.")
        return 0
    rollback(flow)
    flow.step("remove deployment Agent")
    if agent:
        current = agent_target(app)
        d.need(current and current["id"] == agent["id"], "Agent identity changed during offboarding.")
        if current["running"]:
            app.docker("stop", "--time", "30", current["id"])
        app.docker("rm", current["id"])
    if dbm:
        flow.step("remove owned DBM SQL objects")
        dbm_cleanup(app, dbm, retained)
    if installer:
        flow.step("remove owned host SSI and restart Docker")
        # Recheck other containers before a host-wide operation.
        d.need(ssi_plan(app, ownership["ssi"], False, []) == installer, "SSI installer changed during offboarding.")
        app.run([installer, "apm", "uninstrument", "docker"], timeout=300)
        for package in ("datadog-apm-library-php", "datadog-apm-inject"):
            if (Path("/opt/datadog-packages") / package).exists():
                app.run([installer, "remove", package], timeout=300)
        app.run(["systemctl", "restart", "docker"], timeout=180)
        app.wait_probe(["docker", "info"], "Docker daemon after SSI uninstall")
        d.need(app.docker("info", "--format", "{{.DefaultRuntime}}") == "runc",
               "Docker default runtime was not restored; inspect SSI uninstall before continuing.")
        d.need(not any((Path("/opt/datadog-packages") / package).exists() for package in ownership["ssi"]["packages"]),
               "SSI package files remain after uninstall; inspect installer state.")
        flow.recreate("db")
        flow.wait_database()
        for service in ("api", "web"):
            flow.recreate(service)
        ownership["ssi"]["status"] = "removed"
        app.record_offboarding("ssi", ownership["ssi"])
    flow.step("verify offboarded application containers")
    saved = d.read_json(app.out / "recovery.json", secret=True)
    for role in MAPPING:
        verify_recovered(flow, saved, role)
        if role != "mysql":
            app.wait_application(role)
            modules = app.docker("exec", app.c[role + "_container"], "apache2ctl", "-M")
            d.need("datadog_module" not in modules, "RUM module remains in a recovered application; inspect it.")
            app.docker("exec", app.c[role + "_container"], "php", "-r",
                       'exit(getenv("DD_TRACE_ENABLED")==="false" && getenv("DD_INSTRUMENT_SERVICE_WITH_APM")==="false" ? 0 : 1);')
    app.wait_http()
    status = "offboarding_requires_manual_cleanup" if retained else "offboarding_completed"
    d.secure_write(app.out / "offboarding-report.json", d.jdump({"status": status, "retained": retained,
        "local_artifacts": "retained", "datadog_credentials": "revoke separately when no longer shared",
        "application_data": "preserved", "application_override": str(flow.override)}))
    flow.record_status(status)
    for reason in retained:
        d.log(reason, "WARN")
    d.log("Offboarding completed" + (" with manual cleanup required." if retained else "."), "WARN" if retained else "OK")
    d.log("Keep rollback.override.json for future Compose operations. Revoke unused Datadog credentials separately.", "NEXT")
    return 2 if retained else 0
