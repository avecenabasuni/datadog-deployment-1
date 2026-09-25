"""Coordinator tests; one optional real Compose config check, no Docker daemon needed."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_deploy import config, d, report, FakeRunner, ROOT

spec = importlib.util.spec_from_file_location("production", ROOT / "scripts/eminerba_production.py")
p = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"datadog_deploy": d}):
    spec.loader.exec_module(p)


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compose_file = self.root / "docker-compose.yml"
        self.compose_file.write_text("services: {}")
        c = config()
        c.update(output_dir=str(self.root / "generated"), network="actual-project_eminerba_network",
                 mysql_host="db", db_apps=["web", "api"], mysql_config_target="/etc/mysql/conf.d/zz-datadog.cnf")
        for role, (service, container) in p.MAPPING.items():
            c[role + "_container"] = container
            c[role + "_compose_service"] = service
        self.app = d.Deployment(c, {"api_key": "test-key", "db_password": "test-dbm", "admin_password": "test-admin"}, FakeRunner())
        self.flow = p.Production(self.app, self.compose_file)
        self.flow.project = "actual-project"
        self.flow.backend = ["docker", "compose"]
        self.flow.state = {}
        self.report = report()
        self.model = {"services": {}, "volumes": {"mysql_data": {}, "php_sessions": {}},
                      "networks": {"eminerba_network": {"name": c["network"]}}}
        for role, (service, container) in p.MAPPING.items():
            mounts = []
            if role == "mysql":
                mounts = [{"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql"},
                          {"type": "bind", "source": str(self.root / "docker/my.cnf"), "target": "/etc/mysql/conf.d/my.cnf"}]
            else:
                folder = "pit_to_port" if role == "web" else "apiminerba"
                mounts = [{"type": "bind", "source": str(self.root / folder), "target": "/var/www/html"}]
                if role == "web":
                    mounts.append({"type": "volume", "source": "php_sessions", "target": "/var/lib/php/sessions"})
            self.model["services"][service] = {"container_name": container, "volumes": mounts, "networks": {"eminerba_network": {}}}
            item = self.report["selected"][role]
            item.update(service=service, project=self.flow.project, networks={c["network"]: {}}, mounts=[])
            for mount in mounts:
                target, (kind, source) = p.mount_source(self.model, self.flow.project, mount, self.root)
                item["mounts"].append({"Destination": target, "Type": kind, "Name": source,
                                       "Source": source, "RW": True})

    def test_existing_project_named_volume_and_sources_are_validated(self):
        self.flow.validate_stack(self.report, self.model)
        self.assertEqual(self.flow.database_mount, ("volume", "actual-project_mysql_data"))
        changed = copy.deepcopy(self.model)
        changed["volumes"]["mysql_data"] = {"name": "different_mysql_data"}
        with self.assertRaisesRegex(d.Failure, "Live mount differs"):
            self.flow.validate_stack(self.report, changed)

    def test_source_network_and_unknown_mount_changes_refused(self):
        changed = copy.deepcopy(self.model)
        changed["services"]["web"]["volumes"][0]["source"] = str(self.root / "wrong")
        with self.assertRaises(d.Failure):
            self.flow.validate_stack(self.report, changed)
        changed = copy.deepcopy(self.model)
        changed["networks"]["eminerba_network"]["name"] = "wrong_network"
        with self.assertRaises(d.Failure):
            self.flow.validate_stack(self.report, changed)
        self.report["selected"]["api"]["mounts"].append(
            {"Destination": "/important", "Type": "bind", "Source": "/important"})
        with self.assertRaisesRegex(d.Failure, "extra mounts"):
            self.flow.validate_stack(self.report, self.model)

    def test_legacy_mount_syntax_preserves_existing_named_volume(self):
        target, source = p.mount_source(self.model, self.flow.project, "mysql_data:/var/lib/mysql:rw", self.root)
        self.assertEqual((target, source), ("/var/lib/mysql", ("volume", "actual-project_mysql_data")))

    def test_compose_uses_live_project_and_explicit_files_env(self):
        (self.root / ".env").write_text("PRIVATE=do-not-print")
        self.flow.validate_stack(self.report, self.model)
        self.flow.base_digest = p.digest(self.model)
        with patch.object(self.flow, "model", return_value=self.model):
            self.flow.recreate("db")
        commands = [args for args, _ in self.app.r.calls]
        self.assertEqual(len(commands), 2)
        self.assertIn("actual-project", commands[-1])
        self.assertIn(str(self.compose_file), commands[-1])
        self.assertIn(str(self.flow.override), commands[-1])
        self.assertIn(str(self.root / ".env"), commands[-1])
        self.assertEqual(commands[-1][-6:], ["up", "-d", "--no-deps", "--no-build", "--force-recreate", "db"])

    def test_merged_override_cannot_replace_database_volume(self):
        self.flow.validate_stack(self.report, self.model)
        self.flow.base_digest = p.digest(self.model)
        changed = copy.deepcopy(self.model)
        changed["services"]["db"]["volumes"][0]["source"] = "empty_new_volume"
        with patch.object(self.flow, "model", side_effect=[self.model, changed]):
            with self.assertRaisesRegex(d.Failure, "changes an existing mount"):
                self.flow.recreate("db")
        self.assertFalse(any("up" in args for args, _ in self.app.r.calls))

    def test_compose_changes_during_deployment_block_recreation(self):
        self.flow.base_digest = "earlier-digest"
        with patch.object(self.flow, "model", return_value=self.model):
            with self.assertRaisesRegex(d.Failure, "changed during deployment"):
                self.flow.recreate("db")
        self.assertEqual(self.app.r.calls, [])

    def test_dry_run_does_not_render_install_or_recreate(self):
        with patch.object(self.flow, "check", return_value=self.report), \
                patch.object(self.flow, "images") as images, patch.object(self.app, "render") as render, \
                patch.object(self.flow, "recreate") as recreate:
            self.flow.run(dry=True)
        images.assert_not_called()
        render.assert_not_called()
        recreate.assert_not_called()
        self.assertFalse(self.app.out.exists())

    def test_failed_preflight_does_not_start_changes(self):
        with patch.object(self.flow, "check", side_effect=d.Failure("wrong data volume")), \
                patch.object(self.flow, "images") as images, patch.object(self.app, "render") as render:
            with self.assertRaises(d.Failure):
                self.flow.run()
        images.assert_not_called()
        render.assert_not_called()

    def test_database_readiness_is_bounded(self):
        with patch.object(self.app, "mysql", side_effect=d.Failure("not ready")), \
                patch.object(p.time, "monotonic", side_effect=[0, 301]):
            with self.assertRaisesRegex(d.Failure, "readiness timed out"):
                self.flow.wait_database()

    def check_with_mocks(self):
        def docker(*args, **kwargs):
            if "inspect" in args:
                return json.dumps(str(self.compose_file))
            return "core_module (static)"
        with patch.object(self.app, "preflight", return_value=self.report), \
                patch.object(self.app, "artifact"), patch.object(self.app, "docker", side_effect=docker), \
                patch.object(self.app, "mysql", side_effect=lambda sql: "1" if "information_schema.schemata" in sql else "/var/lib/mysql/"), \
                patch.object(self.flow, "model", return_value=self.model):
            return self.flow.check()

    def test_check_discovers_existing_project_without_mutation(self):
        self.assertEqual(self.check_with_mocks(), self.report)
        self.assertEqual(self.flow.project, "actual-project")
        self.assertEqual(self.flow.base_digest, p.digest(self.model))
        self.assertEqual([args for args, _ in self.app.r.calls], [["docker", "compose", "version"]])

    def test_foreign_agent_blocks_before_changes(self):
        self.report["inventory"]["agent_candidates"] = ["other-agent"]
        with self.assertRaisesRegex(d.Failure, "Existing other Agent"):
            self.check_with_mocks()
        self.assertEqual(self.app.r.calls, [])

    def test_missing_schema_is_checked_before_deployment_mutation(self):
        with patch.object(self.app, "check_mysql_schemas", side_effect=d.Failure("missing auxiliary schema")):
            with self.assertRaisesRegex(d.Failure, "missing auxiliary schema"):
                self.check_with_mocks()
        self.assertEqual([args for args, _ in self.app.r.calls], [["docker", "compose", "version"]])

    def test_environment_drift_blocks_without_exposing_values(self):
        self.model["services"]["web"]["environment"] = {"DB_PASS": "fixture-secret"}
        fingerprint = hashlib.sha256(b"fixture-secret\n").hexdigest() + "  -"
        with patch.object(self.app, "docker", return_value=fingerprint) as docker:
            self.flow.check_environment(self.model)
        self.assertNotIn("fixture-secret", str(docker.call_args))
        with patch.object(self.app, "docker", return_value="different-hash  -"):
            with self.assertRaisesRegex(d.Failure, "web/DB_PASS") as error:
                self.flow.check_environment(self.model)
        self.assertNotIn("fixture-secret", str(error.exception))

    def test_prepare_refuses_existing_credentials(self):
        credentials = self.root / "secrets.json"
        credentials.write_text("original")
        with self.assertRaisesRegex(d.Failure, "already exists"):
            p.prepare(self.root / "config.json", credentials, self.compose_file)
        self.assertEqual(credentials.read_text(), "original")
        self.assertFalse((self.root / "config.json").exists())

    def test_prepare_maps_live_stack_without_guessing_project_or_password(self):
        app = Mock()
        lookup = {container: self.report["selected"][role] for role, (_, container) in p.MAPPING.items()}
        app.inspect.side_effect = lambda container: lookup[container]
        app.docker.return_value = json.dumps(["eminerba", "second_database"])
        config_path, secret_path = self.root / "config.json", self.root / "secrets.json"
        with patch.object(p.d, "Deployment", return_value=app):
            p.prepare(config_path, secret_path, self.compose_file)
        prepared = d.read_json(config_path)
        self.assertEqual(prepared["network"], self.app.c["network"])
        self.assertEqual(prepared["mysql_schemas"], ["eminerba", "second_database"])
        self.assertEqual(prepared["mysql_config_target"], "/etc/mysql/conf.d/zz-datadog.cnf")
        self.assertEqual(prepared["db_apps"], ["web", "api"])
        self.assertEqual(d.read_json(secret_path)["admin_password"], "")
        self.assertEqual(len(d.read_json(secret_path)["db_password"]), 48)
        app.fetch.assert_called_once()

    def test_running_images_are_pinned_without_rebuilding_application(self):
        image_id = "sha256:" + "a" * 64
        with patch.object(self.app, "docker", return_value=json.dumps(image_id)) as docker:
            images = self.flow.images()
        self.assertEqual(set(images), {"web", "api", "db"})
        self.assertTrue(all(value.endswith("a" * 64) for value in images.values()))
        self.assertEqual(docker.call_args_list[-1].args[:3], ("image", "tag", image_id))
        self.assertFalse(any("build" in call.args for call in docker.call_args_list))
        self.assertEqual(self.flow.state["original_images"], images)

    def test_missing_tools_builds_only_derived_images(self):
        image_id = "sha256:" + "b" * 64
        def docker(*args, **kwargs):
            if args[0] == "inspect":
                return json.dumps(image_id)
            if args[:2] == ("image", "inspect"):
                return json.dumps("")
            return ""
        with patch.object(self.app.r, "run", return_value=subprocess.CompletedProcess([], 1, "", "")), \
                patch.object(self.app, "docker", side_effect=docker) as mocked:
            images = self.flow.images()
        self.assertIn("db-base", images["db"])
        builds = [call for call in mocked.call_args_list if call.args[0] == "build"]
        self.assertEqual(len(builds), 2)
        for role in ("web", "api"):
            dockerfile = (self.app.out / ("tools-build-" + role) / "Dockerfile").read_text()
            self.assertIn("FROM eminerba-observability/" + role + "-base:" + "b" * 64, dockerfile)
            self.assertIn("gnupg", dockerfile)
            self.assertNotIn("COPY", dockerfile)
        self.assertTrue(all(call.kwargs["timeout"] == 1800 for call in builds))

    def test_existing_rum_requires_managed_persistence(self):
        self.app.out.mkdir()
        state = {"compose_digest": p.digest(self.model), "rum_digest": p.digest({k: self.app.c[k] for k in p.RUM_KEYS})}
        d.secure_write(self.flow.state_path, d.jdump(state))
        def docker(*args, **kwargs):
            return json.dumps(str(self.compose_file)) if "inspect" in args else "datadog_module (shared)"
        with patch.object(self.app, "preflight", return_value=self.report), \
                patch.object(self.app, "artifact"), patch.object(self.app, "docker", side_effect=docker), \
                patch.object(self.app, "mysql", side_effect=lambda sql: "1" if "information_schema.schemata" in sql else "/var/lib/mysql/"), \
                patch.object(self.flow, "model", return_value=self.model):
            with self.assertRaisesRegex(d.Failure, "Existing RUM"):
                self.flow.check()

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI required for real Compose config validation")
    def test_real_compose_parser_preserves_original_mounts_and_environment(self):
        version = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=15)
        if version.returncode:
            self.skipTest("Docker Compose plugin unavailable")
        base = copy.deepcopy(self.model)
        base["version"] = "3.8"
        for service in base["services"].values():
            service["image"] = "fixture-app:current"
        base["services"]["db"]["image"] = "mysql:8.0"
        base["services"]["web"]["environment"] = {"DB_PASS": "${MYSQL_PASSWORD}"}
        (self.root / ".env").write_text("MYSQL_PASSWORD=fixture-only-password\n")
        self.compose_file.write_text(json.dumps(base))
        self.app.render(self.report)
        overlay = d.read_json(self.app.out / "application.override.json")
        for service in overlay["services"].values():
            service.update(image="fixture-pinned:current", runtime="dd-shim")
        overlay["services"]["web"]["volumes"] += [
            "/opt/fixture/apache:/etc/apache2:ro", "/opt/fixture/module:/opt/datadog-httpd:ro"]
        d.secure_write(self.flow.override, d.jdump(overlay))
        command = ["docker", "compose", "--project-directory", str(self.root), "-p", self.flow.project,
                   "--env-file", str(self.root / ".env"), "-f", str(self.compose_file)]
        def model(extra):
            result = subprocess.run(command + extra + ["config", "--format", "json"],
                                    check=True, capture_output=True, text=True, timeout=30, cwd=self.root)
            return json.loads(result.stdout)
        original = model([])
        merged = model(["-f", str(self.flow.override)])
        for name, service in original["services"].items():
            resulting = {mount["target"]: mount for mount in merged["services"][name]["volumes"]}
            for mount in service["volumes"]:
                self.assertEqual(resulting[mount["target"]], mount)
            self.assertEqual(merged["services"][name]["runtime"], "dd-shim")
        self.assertEqual(merged["services"]["web"]["environment"]["DB_PASS"], "fixture-only-password")
        self.assertNotIn("fixture-only-password", self.flow.override.read_text())
        self.assertEqual(original["volumes"]["mysql_data"]["name"], merged["volumes"]["mysql_data"]["name"])

    def test_missing_maintenance_prevents_apply(self):
        # CLI refuses before reading credentials or invoking Docker on Linux.
        with patch.object(p.sys, "platform", "linux"), patch.object(p.os, "geteuid", return_value=0, create=True), \
                patch.object(p, "Production") as production:
            self.assertEqual(p.main(["apply"]), 1)
        production.assert_not_called()

    def test_full_order_and_rum_persistence_after_recreation(self):
        self.flow.base_digest = p.digest(self.model)
        self.flow.database_mount = ("volume", "actual-project_mysql_data")
        events = []
        export = self.app.out / "rum-persistence-test"
        def export_rum(report):
            events.append("export")
            d.secure_write(export / "persistence.override.example.json", d.jdump({"services": {"web": {
                "volumes": [str(export / "apache") + ":/etc/apache2:ro", str(export / "module") + ":/opt/datadog-httpd:ro"]}}}))
            return export
        def recreate(service):
            events.append("recreate-" + service)
            overlay = d.read_json(self.flow.override)
            self.assertEqual(overlay["services"]["db"]["image"], "pinned-db")
            if events.count("recreate-web") == 2:
                self.assertIn(str(export / "apache") + ":/etc/apache2:ro", overlay["services"]["web"]["volumes"])
        with patch.object(self.flow, "check", return_value=self.report), \
                patch.object(self.flow, "images", return_value={"web": "pinned-web", "api": "pinned-api", "db": "pinned-db"}), \
                patch.object(self.flow, "compose"), patch.object(self.flow, "recreate", side_effect=recreate), \
                patch.object(self.flow, "wait_database", side_effect=lambda: events.append("ready")), \
                patch.object(self.app, "start_agent", side_effect=lambda r: events.append("agent")), \
                patch.object(self.app, "install_ssi", side_effect=lambda r: events.append("ssi")), \
                patch.object(self.app, "inspect", return_value=self.report["selected"]["mysql"]), \
                patch.object(self.app, "verify_mysql_startup"), \
                patch.object(self.app, "dbm_sql", side_effect=lambda r: events.append("dbm")), \
                patch.object(self.app, "preflight", return_value=self.report), \
                patch.object(self.app, "install_rum", side_effect=lambda r: events.append("rum")), \
                patch.object(self.app, "export_rum", side_effect=export_rum), \
                patch.object(self.app, "verify", side_effect=lambda r: events.append("verify")), \
                patch.object(self.app, "smoke", side_effect=lambda: events.append("smoke")):
            self.flow.run()
            first_events = list(events)
            events.clear()
            self.flow.run()
            self.assertEqual(events, first_events)
        self.assertEqual(events, ["agent", "ssi", "recreate-db", "ready", "dbm", "recreate-api", "recreate-web",
                                  "rum", "export", "recreate-web", "verify", "smoke"])
        self.assertEqual(d.read_json(self.flow.state_path)["rum_override"], str(export / "persistence.override.example.json"))
        self.assertEqual(len(d.read_json(self.flow.override)["services"]["web"]["volumes"]), 3)


if __name__ == "__main__":
    unittest.main()
