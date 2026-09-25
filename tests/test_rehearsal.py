"""Production-layout lab generation, isolation, and orchestration tests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_deploy import ROOT, d
from test_production import p

spec = importlib.util.spec_from_file_location("rehearsal", ROOT / "scripts/eminerba_lab.py")
lab = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"datadog_deploy": d, "eminerba_production": p}):
    spec.loader.exec_module(lab)


class LabRunner:
    def __init__(self, names="", project=p.LAB_PROJECT):
        self.calls = []
        self.names = names
        self.project = project

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        output, code = "", 0
        if args[:2] == ["docker", "info"]:
            output = '{"SecurityOptions": []}'
        elif args[:3] == ["docker", "context", "inspect"]:
            output = "unix:///var/run/docker.sock"
        elif args[:2] == ["docker", "ps"]:
            output = self.names
        elif args[:2] == ["docker", "inspect"]:
            output = json.dumps(self.project)
        elif args[0] == "systemctl":
            code = 3
        elif args[0] == "curl":
            output = '{"driver":"pdo_mysql","samples":[{},{},{}]}'
        return subprocess.CompletedProcess(args, code, output, "")


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = Path(self.temp.name) / "rehearsal/stack"
        self.config = Path(self.temp.name) / "config.json"
        self.secrets = Path(self.temp.name) / "secrets.json"

    def test_generated_layout_and_credentials_preserved_on_rerun(self):
        lab.create_fixture(self.stack)
        before = (self.stack / ".env").read_bytes()
        lab.create_fixture(self.stack)
        self.assertEqual((self.stack / ".env").read_bytes(), before)
        p.validate_lab_fixture(self.stack / "docker-compose.yml")
        model = d.read_json(self.stack / "docker-compose.yml")
        self.assertEqual(set(model["services"]), {"web", "api", "db"})
        self.assertEqual(model["services"]["web"]["container_name"], "eminerba_web")
        self.assertIn("./pit_to_port:/var/www/html", model["services"]["web"]["volumes"])
        self.assertIn("./apiminerba:/var/www/html", model["services"]["api"]["volumes"])
        self.assertNotIn("ports", model["services"]["db"])
        for role in ("web", "api"):
            self.assertTrue(all(port.startswith("127.0.0.1:") for port in model["services"][role]["ports"]))
            self.assertTrue((self.stack / ("pit_to_port" if role == "web" else "apiminerba") / "health.php").is_file())
        self.assertIn(".env", (self.stack / ".dockerignore").read_text())
        if os.name != "nt":
            self.assertEqual((self.stack / ".env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.stack / "apiminerba").stat().st_mode & 0o777, 0o755)
            self.assertEqual((self.stack / "apiminerba/index.php").stat().st_mode & 0o777, 0o644)

    def test_unrecognized_directory_and_changed_compose_refused(self):
        self.stack.mkdir(parents=True)
        (self.stack / "keep.txt").write_text("production data")
        with self.assertRaisesRegex(d.Failure, "refusing to overwrite"):
            lab.create_fixture(self.stack)
        self.assertEqual((self.stack / "keep.txt").read_text(), "production data")
        clean = Path(self.temp.name) / "clean/stack"
        lab.create_fixture(clean)
        (clean / "docker-compose.yml").write_text('{"services": {}}')
        with self.assertRaisesRegex(d.Failure, "Compose/.env changed"):
            p.validate_lab_fixture(clean / "docker-compose.yml")

    def test_existing_workloads_refused_before_creating_files(self):
        runner = LabRunner(names="eminerba-lab-agent")
        with self.assertRaisesRegex(d.Failure, "Other containers"):
            lab.prepare(self.stack, self.config, self.secrets, runner)
        self.assertFalse(self.stack.exists())

    def test_production_names_cannot_be_taken_over(self):
        runner = LabRunner(names="eminerba_web", project="production")
        with self.assertRaisesRegex(d.Failure, "another project"):
            lab.prepare(self.stack, self.config, self.secrets, runner)
        self.assertFalse(self.stack.exists())

    def test_bootstrap_uses_same_coordinator_lab_profile_and_does_not_rotate(self):
        runner = LabRunner()
        def prepare(config_path, secret_path, compose_file, lab=False):
            self.assertTrue(lab)
            d.secure_write(config_path, d.jdump({"env": "lab"}))
            d.secure_write(secret_path, d.jdump({"admin_password": "", "db_password": "keep-dbm-password"}))
        with patch.object(p, "prepare", side_effect=prepare) as coordinator, patch.object(lab, "repair") as repair:
            lab.prepare(self.stack, self.config, self.secrets, runner)
        repair.assert_called_once_with(self.stack, self.config, self.secrets)
        coordinator.assert_called_once_with(self.config, self.secrets, self.stack / "docker-compose.yml", lab=True)
        env = dict(line.split("=", 1) for line in (self.stack / ".env").read_text().splitlines())
        credentials = d.read_json(self.secrets)
        self.assertEqual(credentials["admin_password"], env["MYSQL_ROOT_PASSWORD"])
        self.assertEqual(credentials["db_password"], "keep-dbm-password")
        before = self.secrets.read_bytes()
        again = LabRunner(names="eminerba_web\neminerba_api\neminerba_db")
        lab.prepare(self.stack, self.config, self.secrets, again)
        self.assertEqual(self.secrets.read_bytes(), before)
        self.assertFalse(any(args[0] == "bash" for args, _ in again.calls))

    def test_apply_delegates_without_reimplementing_deployment(self):
        with patch.object(p, "main", return_value=0) as main:
            self.assertEqual(lab.main(["--maintenance"]), 0)
        main.assert_called_once_with(["apply", "--lab", "--maintenance"])
        with patch.object(p, "main", return_value=0) as main:
            lab.main(["--dry-run"])
        main.assert_called_once_with(["apply", "--lab", "--dry-run"])

    def repair_app(self):
        lab.create_fixture(self.stack)
        d.secure_write(self.config, d.jdump({"env": "lab", "mysql_container": "eminerba_db",
            "mysql_port": 3306, "admin_user": "root", "mysql_schemas": ["eminerba_lab", "eminerba_lab_aux"]}))
        d.secure_write(self.secrets, d.jdump({"admin_password": "fixture-password"}))
        app = Mock()
        def docker(*args):
            if args[:2] == ("context", "inspect"):
                return "unix:///var/run/docker.sock"
            if args[0] == "info":
                return "[]"
            return json.dumps(str(self.stack / "docker-compose.yml"))
        app.docker.side_effect = docker
        app.inspect.return_value = {"project": p.LAB_PROJECT, "service": "db", "running": True,
            "mounts": [{"Destination": "/var/lib/mysql", "Type": "volume", "Name": p.LAB_PROJECT + "_mysql_data", "RW": True}]}
        app.mysql.side_effect = lambda sql: "/var/lib/mysql/" if "@@datadir" in sql else "3"
        return app

    def test_repair_only_adds_auxiliary_fixture_data(self):
        app = self.repair_app()
        with patch.object(d, "Deployment", return_value=app):
            lab.repair(self.stack, self.config, self.secrets)
        changes = [call.args[0] for call in app.mysql.call_args_list if not call.args[0].startswith("SELECT")]
        self.assertEqual(len(changes), 1)
        self.assertIn("CREATE DATABASE IF NOT EXISTS eminerba_lab_aux", changes[0])
        self.assertIn("WHERE a.id IS NULL", changes[0])
        for forbidden in ("DROP", "DELETE", "TRUNCATE", "UPDATE", "ALTER USER"):
            self.assertNotIn(forbidden, changes[0])
        app.check_mysql_schemas.assert_called_once()

    def test_repair_dry_run_has_no_sql_changes(self):
        app = self.repair_app()
        with patch.object(d, "Deployment", return_value=app):
            lab.repair(self.stack, self.config, self.secrets, dry=True)
        self.assertTrue(all(call.args[0].startswith("SELECT") for call in app.mysql.call_args_list))

    def test_repair_refuses_wrong_volume_before_sql(self):
        app = self.repair_app()
        app.inspect.return_value["mounts"][0]["Name"] = "production_mysql_data"
        with patch.object(d, "Deployment", return_value=app):
            with self.assertRaisesRegex(d.Failure, "rehearsal data volume"):
                lab.repair(self.stack, self.config, self.secrets)
        app.mysql.assert_not_called()

    def test_repair_refuses_production_profile(self):
        app = self.repair_app()
        c = d.read_json(self.config)
        c["env"] = "prod"
        d.secure_write(self.config, d.jdump(c))
        with patch.object(d, "Deployment", return_value=app):
            with self.assertRaisesRegex(d.Failure, "only the generated dummy lab"):
                lab.repair(self.stack, self.config, self.secrets)
        app.mysql.assert_not_called()

    def test_generated_profile_uses_lab_tags_and_separate_artifact_paths(self):
        lab.create_fixture(self.stack)
        app = Mock()
        app.inspect.return_value = {"project": p.LAB_PROJECT, "networks": {p.LAB_PROJECT + "_eminerba_network": {}}}
        app.docker.return_value = '["eminerba_lab", "eminerba_lab_aux"]'
        with patch.object(p.d, "Deployment", return_value=app):
            p.prepare(self.config, self.secrets, self.stack / "docker-compose.yml", lab=True)
        c = d.read_json(self.config)
        self.assertEqual(c["env"], "lab")
        self.assertEqual(c["agent_name"], p.LAB_PROJECT + "-agent")
        self.assertEqual(c["output_dir"], str(self.stack.parent / "generated"))
        self.assertEqual(c["artifact_dir"], str(self.stack.parent / "artifacts"))
        self.assertEqual(c["apm_url"], "http://127.0.0.1:8081/api/")
        # The production profile cannot accidentally apply the lab config.
        production = p.Production(d.Deployment(c), self.stack / "docker-compose.yml")
        with self.assertRaisesRegex(d.Failure, "requires env=prod"):
            production.check()
        c["env"] = "prod"
        rehearsal = p.Production(d.Deployment(c), self.stack / "docker-compose.yml", lab=True)
        with self.assertRaisesRegex(d.Failure, "requires env=lab"):
            rehearsal.check()

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI required for Compose parser check")
    def test_real_compose_parser_accepts_fixture_without_publishing_mysql(self):
        version = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=15)
        if version.returncode:
            self.skipTest("Compose plugin unavailable")
        lab.create_fixture(self.stack)
        result = subprocess.run(["docker", "compose", "--project-directory", str(self.stack),
                                 "--env-file", str(self.stack / ".env"), "-p", p.LAB_PROJECT,
                                 "-f", str(self.stack / "docker-compose.yml"), "config", "--format", "json"],
                                capture_output=True, text=True, check=True, timeout=30, cwd=self.stack)
        model = json.loads(result.stdout)
        self.assertFalse(model["services"]["db"].get("ports"))
        self.assertEqual(model["volumes"]["mysql_data"]["name"], p.LAB_PROJECT + "_mysql_data")
        self.assertEqual(model["services"]["api"]["environment"]["DB_PASSWORD"],
                         model["services"]["db"]["environment"]["MYSQL_PASSWORD"])


if __name__ == "__main__":
    unittest.main()
