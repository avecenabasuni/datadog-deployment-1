"""Local tests only. Docker, SQL, installer, and HTTP operations are mocked."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deploy", ROOT / "scripts/datadog_deploy.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def config():
    c = json.loads((ROOT / "config/production.example.json").read_text())
    c.update(web_container="prod-web-1", api_container="prod-api-1", mysql_container="prod-db-1",
             network="prod-net", agent_image="registry.datadoghq.com/agent:7.76.1",
             version="release-123", mysql_host="database", mysql_schemas=["business"],
             rum_application_id="app-id", rum_client_token="public-token",
             rum_remote_configuration_id="remote-id", rum_url="https://web.example/",
             apm_url="https://api.example/read-only")
    return c


def report():
    return {
        "inventory": {"default_runtime": "runc", "ssi_packages": [], "agent_candidates": [],
                      "host_agent_active": False},
        "mysql_version": [8, 0, 40], "apache": "/usr/sbin/apache2ctl",
        "apache_root": "/etc/apache2", "apache_config": "/etc/apache2/apache2.conf",
        "selected": {role: {"service": "service-" + role, "project": "production",
                            "id": "id-" + role, "image": "example:1", "runtime": "runc"}
                     for role in ("web", "api", "mysql")},
        "php": {role: {"ddtrace": "1.6.0"} for role in ("web", "api")},
    }


class FakeRunner:
    def __init__(self, response=""):
        self.calls = []
        self.response = response

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, self.response, "")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = config()
        self.c["output_dir"] = str(Path(self.temp.name) / "generated")
        self.c["artifact_dir"] = str(Path(self.temp.name) / "artifacts")
        self.secrets = {"api_key": "new-test-only-key", "db_password": "a'b\\c$#%;&日本<>",
                        "admin_password": "root-test-only"}
        self.runner = FakeRunner()
        self.app = d.Deployment(self.c, self.secrets, self.runner)
        self.report = report()

    def test_valid_config(self):
        d.validate(config(), full=True)

    @unittest.skipUnless(shutil.which("go"), "Go required to evaluate Docker templates")
    def test_inspect_templates_execute_with_present_missing_and_null_labels(self):
        self.runner.response = "null"
        field_names = list(self.app.inspect("test-container"))
        templates = []
        for args, _ in self.runner.calls:
            self.assertEqual(args[:3], ["docker", "inspect", "--format"])
            self.assertEqual(args[-1], "test-container")
            self.assertNotIn(".Config.Env", args[3])
            templates.append(args[3])
        labels = {
            "com.docker.compose.service": "web",
            "com.docker.compose.project": "lab",
            "id.sucofindo.datadog.managed": "true",
            "id.sucofindo.datadog.spec": 'hash-with-"quote',
        }
        for sample in (labels, {}, None):
            with self.subTest(labels=sample):
                result = subprocess.run(["go", "run", str(ROOT / "tests/inspect_templates.go")],
                                        input=json.dumps({"Templates": templates, "Labels": sample}),
                                        check=True, capture_output=True, text=True, timeout=120)
                values = dict(zip(field_names, map(json.loads, result.stdout.splitlines())))
                for field, label in zip(("service", "project", "managed", "spec"), labels):
                    self.assertEqual(values[field], (sample or {}).get(label, ""))

    def test_reject_invalid_input(self):
        for key, val in (("web_container", "REPLACE_WEB"), ("network", "--host"),
                         ("env", "bad env"), ("mysql_schemas", ["x; DROP DATABASE mysql"]),
                         ("agent_image", "registry.datadoghq.com/agent:7"),
                         ("mysql_port", 0), ("runtime_security", "false"),
                         ("output_dir", "/"), ("mysql_schemas", ["business", "business"]),
                         ("socket_dir", "/tmp/apm"), ("timeout", -1)):
            with self.subTest(key=key, val=val):
                c = config()
                c[key] = val
                with self.assertRaises(d.Failure):
                    d.validate(c, full=True)

    def test_mysql_versions(self):
        for text in ("mysqld Ver 5.7.44 for Linux", "8.0.40", "8.4.3"):
            self.assertTrue(d.mysql_version(text))
        for text in ("5.6.51", "10.11.0-MariaDB", "8.0.40 Percona", "9.1.0", "unknown"):
            with self.assertRaises(d.Failure):
                d.mysql_version(text)

    def test_sql_encoding_round_trip(self):
        for value in ("a'b", "\\'; DROP USER root; --", "$HOME # : %", "é日本<>", 'a"b'):
            encoded = d.literal(value)
            self.assertEqual(encoded[1:-1].replace("''", "'"), value)
        for value in ("new\nline", "\x00", "\r", "\x1a"):
            with self.assertRaises(d.Failure):
                d.literal(value)

    def test_sql_definition_comparison_preserves_literals(self):
        self.assertEqual(d.normalize_sql("BEGIN\n SELECT 1; END"), d.normalize_sql("begin SELECT 1; end"))
        self.assertNotEqual(d.normalize_sql("SELECT 'A B'"), d.normalize_sql("SELECT 'ab'"))

    def test_render_uses_labels_no_secret_in_override(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.app.render(self.report)
        generated = Path(self.c["output_dir"])
        override = json.loads((generated / "application.override.json").read_text())
        self.assertEqual(set(override["services"]), {"service-web", "service-api", "service-mysql"})
        self.assertEqual(override["services"]["service-api"]["environment"]["DD_DBM_PROPAGATION_MODE"], "full")
        self.assertEqual(override["services"]["service-web"]["environment"]["DD_DBM_PROPAGATION_MODE"], "disabled")
        agent_conf = json.loads((generated / "mysql.d/conf.yaml").read_text(encoding="utf-8"))
        self.assertEqual(agent_conf["instances"][0]["password"], self.secrets["db_password"])
        for secret in self.secrets.values():
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(secret, (generated / "application.override.json").read_text())
        if os.name != "nt":
            self.assertEqual((generated / "mysql.d/conf.yaml").stat().st_mode & 0o777, 0o600)
        before = (generated / "mysql.d/conf.yaml").stat()
        self.app.render(self.report)
        after = (generated / "mysql.d/conf.yaml").stat()
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_ino, after.st_ino)

    def test_render_refuses_changed_live_config(self):
        self.app.render(self.report)
        self.app.s["db_password"] = "different-password"
        with self.assertRaises(d.Failure):
            self.app.render(self.report)

    def test_render_mysql_config_permissions_and_existing_mount_inode(self):
        original_chmod = Path.chmod
        changes = []
        def record_chmod(path, mode):
            changes.append((path, mode))
            original_chmod(path, mode)
        with patch.object(Path, "chmod", record_chmod):
            self.app.render(self.report)
        cnf = self.app.out / "99-datadog.cnf"
        self.assertEqual(changes, [(cnf, 0o644)])
        if os.name != "nt":
            self.assertEqual(cnf.stat().st_mode & 0o777, 0o644)
        cnf.chmod(0o600)
        before = cnf.stat()
        changes.clear()
        with patch.object(Path, "chmod", record_chmod):
            self.app.render(self.report)
        self.assertEqual(changes, [(cnf, 0o644)])
        self.assertEqual(cnf.stat().st_ino, before.st_ino)
        self.assertEqual(cnf.stat().st_mtime_ns, before.st_mtime_ns)
        if os.name != "nt":
            self.assertEqual(cnf.stat().st_mode & 0o777, 0o644)
            for name in ("mysql.d/conf.yaml", "agent-spec.json"):
                self.assertEqual((self.app.out / name).stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "Symlink creation requires Windows privileges")
    def test_render_rejects_mysql_config_symlink(self):
        self.app.render(self.report)
        cnf = self.app.out / "99-datadog.cnf"
        target = self.app.out / "private.cnf"
        target.write_text(cnf.read_text())
        target.chmod(0o600)
        cnf.unlink()
        cnf.symlink_to(target)
        with self.assertRaisesRegex(d.Failure, "symlink"):
            self.app.render(self.report)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_missing_compose_label_requires_explicit_mapping(self):
        self.report["selected"]["web"]["service"] = None
        with self.assertRaises(d.Failure):
            self.app.service(self.report, "web")
        self.c["web_compose_service"] = "web"
        self.assertEqual(self.app.service(self.report, "web"), "web")

    def test_conflicting_compose_label_rejected(self):
        self.c["web_compose_service"] = "container-name-is-not-a-service"
        with self.assertRaises(d.Failure):
            self.app.service(self.report, "web")

    def test_dry_run_no_files_or_mutating_commands(self):
        self.app.dry = True
        self.app.render(self.report)
        self.app.fetch()
        self.app.dbm_sql(self.report, export=True)
        self.app.export_rum(self.report)
        self.app.smoke()
        self.app.verify(self.report)
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(Path(self.c["output_dir"]).exists())
        self.assertFalse(Path(self.c["artifact_dir"]).exists())

    def test_mysql_credentials_only_in_stdin(self):
        self.app.mysql("SELECT 1;")
        args, kwargs = self.runner.calls[-1]
        self.assertNotIn(self.secrets["admin_password"], " ".join(args))
        self.assertEqual(kwargs["data"].splitlines()[0], self.secrets["admin_password"])
        self.assertIn(d.SQL_MODE, kwargs["data"])
        self.assertIn("--binary-mode", " ".join(args))
        self.assertNotIn("env", kwargs)

    def test_dba_export_does_not_rotate_or_drop(self):
        self.app.dbm_sql(self.report, export=True)
        sql = (Path(self.c["output_dir"]) / "dbm-dba-review.sql").read_text(encoding="utf-8")
        self.assertIn(d.literal(self.secrets["db_password"]), sql)
        self.assertNotIn("\nDROP ", sql)
        self.assertNotIn("\nALTER USER ", sql)
        self.assertIn("DELIMITER $$", sql)
        self.assertEqual(self.runner.calls, [])

    def mysql_mock(self, existing=False, conflict=False):
        def execute(sql, admin=True):
            if sql == "SELECT VERSION();":
                return "8.0.40"
            if sql == "SELECT @@datadir;":
                return "/var/lib/mysql/"
            if "information_schema.schemata" in sql:
                return "1"
            if "COUNT(*) FROM mysql.user" in sql:
                return "1" if existing else "0"
            if sql == "SELECT 1;":
                return "1"
            if sql == "SELECT CURRENT_USER();":
                return "datadog@%"
            if "max_user_connections" in sql and sql.startswith("SELECT"):
                return "5"
            if "information_schema.routines" in sql:
                if conflict:
                    return "DEFINER|BEGIN SELECT 'unexpected'; END"
                if existing:
                    for (schema, name), ddl in self.app.procedures().items():
                        if d.literal(schema) in sql and d.literal(name) in sql:
                            return "DEFINER|" + ddl[ddl.index("BEGIN"):]
                return ""
            if "information_schema.parameters" in sql:
                return "IN:text" if "explain_statement" in sql else ""
            return ""
        return Mock(side_effect=execute)

    def test_dbm_fresh_and_rerun(self):
        self.app.mysql = self.mysql_mock()
        self.app.dbm_sql(self.report)
        commands = [call.args[0] for call in self.app.mysql.call_args_list]
        self.assertTrue(any("CREATE USER" in s for s in commands))
        self.assertEqual(sum(s.startswith("DELIMITER") for s in commands), 3)
        self.app.mysql = self.mysql_mock(existing=True)
        self.app.dbm_sql(self.report)
        commands = [call.args[0] for call in self.app.mysql.call_args_list]
        self.assertFalse(any("CREATE USER" in s or s.startswith("DELIMITER") or "ALTER USER" in s for s in commands))

    def test_existing_procedure_conflict_no_mutation(self):
        self.app.mysql = self.mysql_mock(conflict=True)
        with self.assertRaises(d.Failure):
            self.app.dbm_sql(self.report)
        self.assertTrue(all(call.args[0].startswith("SELECT") for call in self.app.mysql.call_args_list))

    def test_dbm_dry_run_only_selects(self):
        self.app.dry = True
        self.app.mysql = self.mysql_mock()
        self.app.dbm_sql(self.report)
        self.assertTrue(all(call.args[0].startswith("SELECT") for call in self.app.mysql.call_args_list))

    def test_optional_features_privileges_independent(self):
        self.assertEqual(self.app.agent_spec()["caps"], [])
        for feature, env in (("runtime_security", "DD_RUNTIME_SECURITY_CONFIG_ENABLED"),
                             ("network_monitoring", "DD_SYSTEM_PROBE_NETWORK_ENABLED"),
                             ("universal_service_monitoring", "DD_SYSTEM_PROBE_SERVICE_MONITORING_ENABLED")):
            c = copy.deepcopy(self.c)
            c[feature] = True
            spec = d.Deployment(c).agent_spec()
            self.assertEqual(spec["environment"][env], "true")
            self.assertEqual(spec["caps"], d.CAPS)
            self.assertTrue(spec["host_cgroup"])
            self.assertNotIn("KILL", spec["caps"])

    def test_agent_existing_never_replaced(self):
        self.app.render(self.report)
        self.report["inventory"]["agent_candidates"] = ["foreign-agent"]
        with self.assertRaises(d.Failure):
            self.app.start_agent(self.report)
        self.assertEqual(self.runner.calls, [])

    def test_host_agent_blocks_second_agent(self):
        self.app.render(self.report)
        self.report["inventory"]["host_agent_active"] = True
        with self.assertRaises(d.Failure):
            self.app.start_agent(self.report)

    def test_ssi_existing_skipped(self):
        self.report["inventory"].update(default_runtime="dd-shim", ssi_packages=["datadog-apm-library-php 1.6"])
        self.app.install_ssi(self.report)
        self.assertEqual(self.runner.calls, [])

    def test_ssi_partial_install_blocks(self):
        self.report["inventory"]["ssi_packages"] = ["datadog-apm-inject 1.0"]
        with self.assertRaises(d.Failure):
            self.app.install_ssi(self.report)

    def test_artifact_checksum(self):
        path = Path(self.c["artifact_dir"]) / "ssi.sh"
        d.secure_write(path, "#!/bin/sh\nexit 0\n")
        self.c["ssi_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.app.artifact("ssi"), path)
        d.secure_write(path, "#!/bin/sh\nexit 1\n")
        with self.assertRaises(d.Failure):
            self.app.artifact("ssi")

    def test_inspect_whitelist(self):
        self.runner.response = "null"
        self.app.inspect("web")
        for args, _ in self.runner.calls:
            self.assertIn("--format", args)
            self.assertNotIn(".Config.Env", " ".join(args))
            self.assertNotIn("{{json .}}", args)

    def test_mysql_check_error_nested(self):
        self.assertTrue(d.has_check_error([{"runner": {"errors": ["access denied"]}}]))
        self.assertFalse(d.has_check_error([{"runner": {"errors": []}}]))

    def test_critical_command_failure_does_not_leak(self):
        secret = "very-sensitive"
        with patch.object(d.subprocess, "run", return_value=subprocess.CompletedProcess(["mysql"], 1, secret, secret)):
            with self.assertRaises(d.Failure) as error:
                d.Runner().run(["mysql"])
            self.assertNotIn(secret, str(error.exception))

    def test_timeout_nonzero(self):
        with patch.object(d.subprocess, "run", side_effect=subprocess.TimeoutExpired(["mysql"], 1)):
            with self.assertRaises(d.Failure):
                d.Runner().run(["mysql"])

    def test_main_missing_config_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.main(["preflight", "--config", str(Path(self.temp.name) / "absent")]), 1)

    def test_maintenance_gate(self):
        path = Path(self.temp.name) / "production.json"
        d.secure_write(path, d.jdump(self.c))
        with patch.object(d.Deployment, "preflight", return_value=self.report), \
                patch.object(d.Deployment, "install_ssi") as install, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.main(["ssi-install", "--config", str(path)]), 1)
            install.assert_not_called()

    def test_ssi_dry_run_does_not_execute_installer(self):
        self.app.dry = True
        self.app.artifact = Mock(return_value=Path("/reviewed/ssi.sh"))
        self.app.install_ssi(self.report)
        self.assertEqual(self.runner.calls, [])

    def test_ssi_exact_host_environment(self):
        self.app.artifact = Mock(return_value=Path("/reviewed/ssi.sh"))
        with patch.object(d.os, "geteuid", return_value=0, create=True), \
                patch.object(self.app, "run", return_value=""), \
                patch.object(self.app, "docker", return_value="dd-shim"):
            self.app.install_ssi(self.report)
            call = self.app.run.call_args
            self.assertEqual(call.args[0], ["bash", str(Path("/reviewed/ssi.sh"))])
            env = call.kwargs["env"]
            self.assertEqual(env["DD_NO_AGENT_INSTALL"], "true")
            self.assertEqual(env["DD_APM_INSTRUMENTATION_ENABLED"], "docker")
            self.assertEqual(env["DD_APM_INSTRUMENTATION_LIBRARIES"], "php:1")
            self.assertNotIn("DD_API_KEY", env)

    def test_fetch_failure_never_executes(self):
        with patch.object(d.urllib.request, "urlopen", side_effect=OSError("download failed")):
            with self.assertRaises(d.Failure):
                self.app.fetch()
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(Path(self.c["artifact_dir"]).exists())

    def test_rum_existing_skips_install(self):
        self.app.artifact = Mock(return_value=Path("/reviewed/rum.sh"))
        with patch.object(self.app, "docker", return_value="datadog_module (shared)") as docker:
            self.app.install_rum(self.report)
            self.assertEqual(len(docker.call_args_list), 2)
            self.assertTrue(all(call.args[0] == "exec" for call in docker.call_args_list))

    def test_rum_dry_run_no_backup_reload(self):
        self.app.dry = True
        self.app.artifact = Mock(return_value=Path("/reviewed/rum.sh"))
        with patch.object(self.app, "docker", return_value="core_module (static)") as docker:
            self.app.install_rum(self.report)
            self.assertEqual(len(docker.call_args_list), 2)
            self.assertFalse(Path(self.c["output_dir"]).exists())

    def test_rum_configtest_failure_prevents_reload(self):
        self.app.artifact = Mock(return_value=Path("/reviewed/rum.sh"))
        def docker(*args, **kw):
            if "mktemp" in args:
                return "/tmp/dd-rum.12345678"
            if "--help" in args:
                return "proxyKind appId site clientToken remoteConfigurationId agentUri"
            if "-t" in args:
                raise d.Failure("invalid apache config")
            return ""
        with patch.object(self.app, "docker", side_effect=docker) as mocked:
            with self.assertRaises(d.Failure):
                self.app.install_rum(self.report)
            self.assertFalse(any("graceful" in call.args for call in mocked.call_args_list))
            self.assertTrue(any("cp" == call.args[0] for call in mocked.call_args_list))

    def test_rum_flags_mismatch_prevents_install(self):
        self.app.artifact = Mock(return_value=Path("/reviewed/rum.sh"))
        def docker(*args, **kw):
            return "/tmp/dd-rum.12345678" if "mktemp" in args else ""
        with patch.object(self.app, "docker", side_effect=docker) as mocked:
            with self.assertRaises(d.Failure):
                self.app.install_rum(self.report)
            self.assertFalse(any("--proxyKind" in call.args for call in mocked.call_args_list))

    def test_rum_help_accepts_flags_on_either_output_stream(self):
        help_flags = "proxyKind appId site clientToken remoteConfigurationId agentUri"
        for stdout, stderr in ((help_flags, ""), ("download complete", help_flags),
                               ("proxyKind appId site", "clientToken remoteConfigurationId agentUri")):
            with self.subTest(stdout=stdout, stderr=stderr):
                self.app.artifact = Mock(return_value=Path("/reviewed/rum.sh"))
                self.app.export_rum = Mock()
                real_docker = self.app.docker
                def docker(*args, **kw):
                    if "mktemp" in args:
                        return "/tmp/dd-rum.12345678"
                    if "--help" in args:
                        return real_docker(*args, **kw)
                    return ""
                with patch.object(self.runner, "run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout, stderr)), patch.object(self.app, "docker", side_effect=docker) as mocked:
                    self.app.install_rum(self.report)
                self.assertTrue(any("--proxyKind" in call.args for call in mocked.call_args_list))
                self.assertTrue(any("graceful" in call.args for call in mocked.call_args_list))
                self.app.export_rum.assert_called_once_with(self.report)

    def test_rum_help_missing_flags_on_both_streams_prevents_install(self):
        self.app.artifact = Mock(return_value=Path("/reviewed/rum.sh"))
        real_docker = self.app.docker
        def docker(*args, **kw):
            if "mktemp" in args:
                return "/tmp/dd-rum.12345678"
            if "--help" in args:
                return real_docker(*args, **kw)
            return ""
        with patch.object(self.runner, "run", return_value=subprocess.CompletedProcess(
                [], 0, "download complete", "appId site")), \
                patch.object(self.app, "docker", side_effect=docker) as mocked:
            with self.assertRaisesRegex(d.Failure, "proxyKind"):
                self.app.install_rum(self.report)
        self.assertFalse(any("--proxyKind" in call.args or "graceful" in call.args
                             for call in mocked.call_args_list))

    def test_agent_managed_rerun_health_only(self):
        self.app.render(self.report)
        body = (self.app.out / "mysql.d/conf.yaml").read_text(encoding="utf-8")
        fingerprint = hashlib.sha256((d.jdump(self.app.agent_spec()) + body
                                      + self.secrets["api_key"]).encode()).hexdigest()
        self.report["inventory"]["agent_candidates"] = [self.c["agent_name"]]
        with patch.object(self.app, "inspect", return_value={
                "managed": "true", "spec": fingerprint, "running": True}):
            self.app.start_agent(self.report)
        self.assertEqual(self.runner.calls[0][0], ["docker", "exec", self.c["agent_name"], "agent", "health"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_agent_health_timeout(self):
        self.runner.run = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
        with patch.object(d.time, "monotonic", side_effect=[0, self.c["timeout"] + 1]):
            with self.assertRaises(d.Failure):
                self.app.wait_agent()

    def test_preflight_is_read_only_and_no_compose_required(self):
        c = config()
        app = d.Deployment(c, {}, FakeRunner())
        inv = report()["inventory"]
        inv.update(rootless=False, kernel="6.8.0")
        selected = {role: {"running": True, "networks": {"prod-net": {"Aliases": ["database"]}},
                           "mounts": [{"Type": "volume", "RW": True, "Destination": "/var/lib/mysql"}]}
                    for role in ("web", "api", "mysql")}
        php = {"version": "8.1.31", "pdo": ["mysql"], "mysqli": True,
               "conflicts": [], "jit": "off", "jit_buffer": "0", "ddtrace": None}
        def docker(*args, **kw):
            if args[0] == "context":
                return "unix:///var/run/docker.sock"
            if "mysqld" in args:
                return "mysqld Ver 8.0.40 for Linux"
            if d.PHP_PROBE in args:
                return json.dumps(php)
            if d.APACHE_PROBE in args:
                return "/usr/sbin/apache2ctl"
            if "-V" in args:
                return '-D HTTPD_ROOT="/etc/apache2"\n-D SERVER_CONFIG_FILE="apache2.conf"'
            return ""
        with patch.object(d.platform, "system", return_value="Linux"), \
                patch.object(d.platform, "machine", return_value="x86_64"), \
                patch.object(d.shutil, "which", return_value="/usr/bin/tool"), \
                patch.object(d.Path, "read_text", return_value='ID=ubuntu\nVERSION_ID="22.04"'), \
                patch.object(app, "discover", return_value=inv), \
                patch.object(app, "inspect", side_effect=lambda name: selected[
                    {"prod-web-1": "web", "prod-api-1": "api", "prod-db-1": "mysql"}[name]]), \
                patch.object(app, "docker", side_effect=docker) as mocked, \
                patch.object(d, "secure_write") as write:
            found = app.preflight(full=True)
            self.assertEqual(found["mysql_version"], [8, 0, 40])
            write.assert_not_called()
            for call in mocked.call_args_list:
                self.assertNotIn(call.args[0], ("run", "cp", "restart", "stop", "rm"))
                self.assertNotIn("graceful", call.args)

    def test_no_app_database_lifecycle_code(self):
        source = (ROOT / "scripts/datadog_deploy.py").read_text(encoding="utf-8")
        self.assertNotIn("datadog-setup.php", source)
        self.assertNotIn("force-recreate", source)
        self.assertNotIn('self.docker("rm"', source)
        self.assertNotIn('self.docker("restart"', source)


if __name__ == "__main__":
    unittest.main()
