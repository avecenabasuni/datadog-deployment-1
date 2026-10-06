"""Offboarding ownership, SQL protection, dry-run and lifecycle regressions; no real servers."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from test_deploy import ROOT
from test_production import d, p, recovery
import test_recovery as r
from test_rehearsal import lab

spec = importlib.util.spec_from_file_location("eminerba_offboarding", ROOT / "scripts/eminerba_offboarding.py")
o = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"datadog_deploy": d, "eminerba_production": p, "eminerba_recovery": recovery}):
    spec.loader.exec_module(o)


class OffboardingTests(unittest.TestCase):
    setUp = r.RecoveryTests.setUp
    baseline = r.RecoveryTests.baseline

    def dbm(self, created=True):
        return {"status": "installed", "user_created": created, "schema_created": False,
                "account_fingerprint": hashlib.sha256(b"test-account").hexdigest(),
                "grants_fingerprint": hashlib.sha256(b"test-grants").hexdigest(),
                "procedures": [{"schema": schema, "name": name,
                                "definer": "root@localhost",
                                "body": d.normalize_sql(ddl[ddl.index("BEGIN"):]),
                                "parameters": "IN:text" if name == "explain_statement" else ""}
                               for (schema, name), ddl in self.app.procedures().items()]}

    def sql(self, sql, **kwargs):
        if sql.startswith("SELECT COUNT(*) FROM mysql.user"):
            return "1"
        if "CONCAT_WS('|',plugin,authentication_string,max_user_connections)" in sql:
            return "test-account"
        if sql.startswith("SHOW GRANTS"):
            return "test-grants"
        if "CONCAT(security_type" in sql:
            for (schema, name), ddl in self.app.procedures().items():
                if d.literal(schema) in sql and d.literal(name) in sql:
                    return "DEFINER|" + ddl[ddl.index("BEGIN"):]
        if "information_schema.parameters" in sql:
            return "IN:text" if "explain_statement" in sql else ""
        if sql.startswith("SELECT definer FROM"):
            return "root@localhost"
        if sql.startswith("SELECT COUNT(*)"):
            return "0"
        return ""

    def test_dbm_owned_objects_are_removed_without_application_schema_or_data_deletion(self):
        saved = self.dbm()
        with patch.object(self.app, "mysql", side_effect=self.sql) as mysql:
            o.dbm_cleanup(self.app, saved, [])
        mutations = [call.args[0] for call in mysql.call_args_list if call.args[0].startswith("DROP")]
        self.assertEqual(len(mutations), 4)
        self.assertIn("DROP USER IF EXISTS 'datadog'@'%';", mutations)
        self.assertIn("DROP PROCEDURE IF EXISTS `business`.`explain_statement`;", mutations)
        self.assertFalse(any("DROP SCHEMA" in sql or "DROP TABLE" in sql or "DELETE" in sql for sql in mutations))
        self.assertEqual(self.app.offboarding_manifest()["dbm"]["status"], "removed")

    def test_preexisting_dbm_account_is_never_dropped(self):
        saved = self.dbm(created=False)
        retained = []
        self.assertIs(o.dbm_plan(self.app, saved, False, retained), saved)
        with patch.object(self.app, "mysql", side_effect=self.sql) as mysql:
            o.dbm_cleanup(self.app, saved, retained)
        self.assertFalse(any("DROP USER" in call.args[0] for call in mysql.call_args_list))
        self.assertTrue(retained)
        rerun = []
        o.dbm_plan(self.app, saved, False, rerun)
        self.assertTrue(rerun)

    def test_dbm_dry_run_has_no_mutating_sql_or_file_writes(self):
        with patch.object(self.app, "mysql", side_effect=self.sql) as mysql, \
                patch.object(self.app, "record_offboarding") as record:
            o.dbm_cleanup(self.app, self.dbm(), [], dry=True)
        self.assertTrue(all(call.args[0].startswith(("SELECT", "SHOW GRANTS")) for call in mysql.call_args_list))
        record.assert_not_called()

    def test_changed_account_grants_routine_or_shared_privilege_blocks_all_drops(self):
        for marker, output in (("CONCAT_WS", "changed-account"), ("SHOW GRANTS", "changed-grants"),
                               ("CONCAT(security_type", "DEFINER|BEGIN SELECT 42; END"),
                               ("information_schema.parameters", "IN:int"),
                               ("SELECT definer FROM", "another@localhost"),
                               ("information_schema.routine_privileges", "1"),
                               ("WHERE definer=", "1")):
            with self.subTest(marker=marker):
                def execute(sql, **kwargs):
                    return output if marker in sql else self.sql(sql, **kwargs)
                with patch.object(self.app, "mysql", side_effect=execute) as mysql:
                    with self.assertRaises(d.Failure):
                        o.dbm_cleanup(self.app, self.dbm(), [])
                self.assertFalse(any(call.args[0].startswith("DROP") for call in mysql.call_args_list))

    def test_only_owned_empty_datadog_schema_can_be_dropped(self):
        for occupied in (False, True):
            with self.subTest(occupied=occupied):
                saved = self.dbm()
                saved["schema_created"] = True
                retained = []
                def execute(sql, **kwargs):
                    if "FROM information_schema.tables" in sql:
                        return "1" if occupied else "0"
                    return self.sql(sql, **kwargs)
                with patch.object(self.app, "mysql", side_effect=execute) as mysql:
                    o.dbm_cleanup(self.app, saved, retained)
                self.assertEqual(any(call.args[0] == "DROP SCHEMA IF EXISTS datadog;" for call in mysql.call_args_list), not occupied)
                self.assertEqual(bool(retained), occupied)

    def test_ownership_context_and_modified_procedure_inventory_are_rejected(self):
        self.app.record_offboarding("dbm", self.dbm())
        self.app.c["db_user"] = "different"
        with self.assertRaisesRegex(d.Failure, "ownership differs"):
            self.app.offboarding_manifest()
        self.app.c["db_user"] = "datadog"
        saved = self.dbm()
        saved["procedures"][0]["schema"] = "mysql"
        with self.assertRaisesRegex(d.Failure, "procedure differs"):
            o.dbm_plan(self.app, saved, False, [])
        saved = self.dbm()
        saved["status"] = "provisioning"
        with self.assertRaisesRegex(d.Failure, "incomplete"):
            o.dbm_plan(self.app, saved, False, [])

    def agent(self):
        return {"id": "agent-id", "managed": "true", "spec": "f" * 64, "running": True,
                "image": self.app.c["agent_image"], "mounts": [{"Type": "bind",
                "Source": str(self.app.out / "mysql.d/conf.yaml"),
                "Destination": "/etc/datadog-agent/conf.d/mysql.d/conf.yaml"}]}

    def test_foreign_agent_or_configuration_mount_is_rejected(self):
        for changed in ({"managed": "false"}, {"image": "another:1"}, {"mounts": []}, {"spec": "bad"}):
            with self.subTest(changed=changed):
                item = self.agent()
                item.update(changed)
                with patch.object(self.app, "docker", return_value=self.app.c["agent_name"]), \
                        patch.object(self.app, "inspect", return_value=item):
                    with self.assertRaisesRegex(d.Failure, "Agent does not match"):
                        o.agent_target(self.app)

    def test_shared_ssi_blocks_docker_restart_before_mutation(self):
        saved = {"status": "installed", "original_runtime": "runc",
                 "packages": ["datadog-apm-inject", "datadog-apm-library-php"]}
        with patch.object(self.app, "docker", return_value="another-app") as docker, \
                patch.object(self.app, "inspect", return_value={"running": True, "runtime": "runc", "mounts": []}), \
                patch.object(self.app, "run") as run:
            with self.assertRaisesRegex(d.Failure, "Other containers"):
                o.ssi_plan(self.app, saved, False, [])
        run.assert_not_called()
        self.assertTrue(all(call.args[0] == "ps" for call in docker.call_args_list))

    def test_legacy_ssi_is_retained_without_assuming_ownership(self):
        retained = []
        with patch.object(self.app, "docker", return_value="dd-shim"), patch.object(self.app, "run", return_value="") as run:
            self.assertIsNone(o.ssi_plan(self.app, None, False, retained))
        self.assertTrue(retained)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "dpkg-query")

    def test_offboard_dry_run_executes_real_recovery_checks_without_writes(self):
        self.baseline()
        selected = {name: self.report["selected"][role] for role, (_, name) in p.MAPPING.items()}
        with patch.object(self.app, "docker", side_effect=lambda *args, **kwargs: r.RecoveryTests.docker(self, *args, **kwargs)), \
                patch.object(self.app, "inspect", side_effect=lambda name: selected[name]), \
                patch.object(self.flow, "model", return_value=self.model), \
                patch.object(self.flow, "recreate") as recreate, patch.object(d, "secure_write") as write:
            self.assertEqual(o.offboard(self.flow, dry=True), 0)
        recreate.assert_not_called()
        write.assert_not_called()
        self.assertFalse((self.app.out / "offboarding-report.json").exists())

    def test_full_offboarding_order_and_restart_rechecks_recovered_mounts(self):
        self.baseline()
        self.app.record_offboarding("dbm", self.dbm())
        self.app.record_offboarding("ssi", {"status": "installed", "original_runtime": "runc",
                                            "packages": ["datadog-apm-inject", "datadog-apm-library-php"]})
        events = []
        packages = {"datadog-apm-inject", "datadog-apm-library-php"}
        real_exists = Path.exists
        def exists(path):
            if str(path).replace("\\", "/").startswith("/opt/datadog-packages/"):
                return path.name in packages
            return real_exists(path)
        def recover(flow, dry=False):
            events.append("recovery-check" if dry else "rollback")
            flow.override = self.app.out / "rollback.override.json"
            flow.journal_enabled = not dry
        def run(args, **kwargs):
            events.append(" ".join(args))
            if args[1] == "remove":
                packages.discard(args[2])
            return ""
        def sql_cleanup(app, saved, retained, dry=False):
            events.append("sql-check" if dry else "sql-cleanup")
        with patch.object(Path, "exists", exists), patch.object(o, "rollback", side_effect=recover), \
                patch.object(o, "agent_target", return_value=self.agent()), \
                patch.object(o, "ssi_plan", return_value="/usr/bin/datadog-installer"), \
                patch.object(o, "dbm_cleanup", side_effect=sql_cleanup), \
                patch.object(o, "verify_recovered", side_effect=lambda f, s, role: events.append("verify-" + role)), \
                patch.object(self.app, "docker", side_effect=lambda *a, **k: events.append(" ".join(a)) or
                             (self.app.c["mysql_container"] if a[0] == "ps" else "runc" if a[0] == "info" else "")), \
                patch.object(self.app, "inspect", return_value={"running": True}), \
                patch.object(self.app, "run", side_effect=run), \
                patch.object(self.app, "wait_probe", side_effect=lambda *a: events.append("docker-ready")), \
                patch.object(self.app, "wait_application"), patch.object(self.app, "wait_http"), \
                patch.object(self.flow, "recreate", side_effect=lambda service: events.append("up-" + service)), \
                patch.object(self.flow, "wait_database", side_effect=lambda: events.append("db-ready")):
            self.assertEqual(o.offboard(self.flow), 0)
        self.assertLess(events.index("rollback"), events.index("stop --time 30 agent-id"))
        self.assertLess(events.index("rm agent-id"), events.index("sql-cleanup"))
        self.assertLess(events.index("sql-cleanup"), events.index("/usr/bin/datadog-installer apm uninstrument docker"))
        self.assertLess(events.index("/usr/bin/datadog-installer remove datadog-apm-library-php"),
                        events.index("/usr/bin/datadog-installer remove datadog-apm-inject"))
        self.assertLess(events.index("/usr/bin/datadog-installer remove datadog-apm-inject"),
                        events.index("systemctl restart docker"))
        self.assertLess(events.index("systemctl restart docker"), events.index("up-db"))
        self.assertLess(events.index("up-db"), events.index("db-ready"))
        self.assertLess(events.index("db-ready"), events.index("up-api"))
        self.assertLess(events.index("up-api"), events.index("up-web"))
        self.assertLess(events.index("up-web"), events.index("verify-mysql"))
        self.assertEqual(self.app.offboarding_manifest()["ssi"]["status"], "removed")
        self.assertEqual(d.read_json(self.app.out / "offboarding-report.json")["status"], "offboarding_completed")

    def test_legacy_offboarding_reports_manual_cleanup_and_preserves_sql(self):
        self.baseline()
        with patch.object(o, "rollback"), patch.object(o, "agent_target", return_value=None), \
                patch.object(o, "verify_recovered"), patch.object(self.app, "docker", return_value="runc"), \
                patch.object(self.app, "run", return_value=""), patch.object(self.app, "wait_application"), \
                patch.object(self.app, "wait_http"), patch.object(self.app, "mysql") as mysql:
            self.assertEqual(o.offboard(self.flow), 2)
        mysql.assert_not_called()
        report = d.read_json(self.app.out / "offboarding-report.json")
        self.assertEqual(report["status"], "offboarding_requires_manual_cleanup")
        self.assertTrue(report["retained"])

    def test_failed_application_rollback_prevents_component_removal(self):
        with patch.object(o, "rollback", side_effect=[None, d.Failure("DB rollback failed")]), \
                patch.object(o, "agent_target", return_value=self.agent()), \
                patch.object(o, "ssi_plan", return_value=None), \
                patch.object(o, "dbm_plan", return_value=None), \
                patch.object(self.app, "docker") as docker:
            with self.assertRaisesRegex(d.Failure, "DB rollback failed"):
                o.offboard(self.flow)
        docker.assert_not_called()

    def test_offboarding_requires_maintenance_and_cli_forwards_keep_options(self):
        with patch.object(p.sys, "platform", "linux"), patch.object(p.os, "geteuid", return_value=0, create=True), \
                patch.object(p, "Production") as production:
            self.assertEqual(p.main(["offboard"]), 1)
        production.assert_not_called()
        with patch.dict(sys.modules, {"eminerba_offboarding": o}), \
                patch.object(p.sys, "platform", "linux"), patch.object(p.os, "geteuid", return_value=0, create=True), \
                patch.object(d, "deployment_lock"), patch.object(d, "read_json", return_value=self.app.c), \
                patch.object(d, "Deployment", return_value=self.app), \
                patch.object(p, "Production", return_value=self.flow), patch.object(o, "offboard", return_value=2) as offboard:
            self.assertEqual(p.main(["offboard", "--maintenance", "--keep-ssi", "--keep-dbm"]), 2)
        offboard.assert_called_once_with(self.flow, dry=False, keep_ssi=True, keep_dbm=True)
        with patch.object(lab.p, "main", return_value=2) as main:
            self.assertEqual(lab.main(["offboard", "--dry-run", "--keep-ssi", "--keep-dbm"]), 2)
        main.assert_called_once_with(["offboard", "--lab", "--dry-run", "--keep-ssi", "--keep-dbm"])


if __name__ == "__main__":
    unittest.main()
