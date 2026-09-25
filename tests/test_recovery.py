"""Recovery checks use stopped containers and do not depend on deployment preflight."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

import test_production as production
from test_production import p, d, recovery

class RecoveryTests(unittest.TestCase):
    setUp = production.ProductionTests.setUp

    def baseline(self):
        self.flow.validate_stack(self.report, self.model)
        self.flow.base_digest = p.digest(self.model)
        for mounts in self.flow.preserved_mounts.values():
            for kind, source in mounts.values():
                if kind == "bind":
                    from pathlib import Path
                    Path(source).mkdir(parents=True, exist_ok=True)
        self.image = "sha256:" + "a" * 64
        with patch.object(self.app, "docker", return_value=json.dumps(self.image)):
            recovery.snapshot(self.flow, self.report)
        self.saved = d.read_json(self.app.out / "recovery.json")
        for item in self.report["selected"].values():
            item["running"] = True
            item["runtime"] = "runc"

    def docker(self, *args, **kwargs):
        if args[:2] == ("context", "inspect"):
            return "unix:///var/run/docker.sock"
        if args[:2] == ("info", "--format"):
            return json.dumps({"runc": {}}) if "Runtimes" in args[2] else "[]"
        if args[0] == "ps":
            return "\n".join(container for _, container in p.MAPPING.values())
        if args[0] == "inspect" and "project.config_files" in args[2]:
            return json.dumps(str(self.compose_file))
        if args[:2] == ("image", "inspect") or args[0] == "inspect":
            return json.dumps(self.image)
        return ""

    def recover(self, dry=False):
        selected = {container: self.report["selected"][role] for role, (_, container) in p.MAPPING.items()}
        with patch.object(self.app, "docker", side_effect=self.docker), \
                patch.object(self.app, "inspect", side_effect=lambda name: selected[name]), \
                patch.object(self.flow, "model", return_value=self.model), \
                patch.object(self.app, "preflight", side_effect=AssertionError("must not need live preflight")):
            return recovery.rollback(self.flow, dry=dry)

    def test_snapshot_is_immutable_and_contains_no_credentials(self):
        self.baseline()
        contents = (self.app.out / "recovery.json").read_text()
        with patch.object(self.app, "docker", return_value=json.dumps(self.image)) as docker:
            recovery.snapshot(self.flow, self.report)
        self.assertTrue(all(call.args[:2] == ("image", "inspect") for call in docker.call_args_list))
        self.assertEqual((self.app.out / "recovery.json").read_text(), contents)
        for secret in self.app.s.values():
            self.assertNotIn(secret, contents)
        self.flow.base_digest = "different"
        with self.assertRaisesRegex(d.Failure, "baseline differs"):
            recovery.snapshot(self.flow, self.report)

    def test_rollback_dry_run_accepts_stopped_containers_without_writes(self):
        self.baseline()
        for item in self.report["selected"].values():
            item["running"] = False
        with patch.object(self.flow, "recreate") as recreate:
            self.recover(dry=True)
        recreate.assert_not_called()
        self.assertFalse((self.app.out / "rollback.override.json").exists())
        self.assertFalse((self.app.out / "deployment-status.json").exists())

    def test_rollback_recreates_database_before_apps_and_disables_instrumentation(self):
        self.baseline()
        events = []
        with patch.object(self.flow, "recreate", side_effect=lambda service: events.append(service)), \
                patch.object(self.flow, "wait_database", side_effect=lambda: events.append("sql-ready")):
            self.recover()
        self.assertEqual(events, ["db", "sql-ready", "api", "web"])
        overlay = d.read_json(self.app.out / "rollback.override.json")
        for service in overlay["services"].values():
            self.assertEqual(service["image"], self.image)
            self.assertEqual(service["runtime"], "runc")
            self.assertEqual(service["environment"]["DD_INSTRUMENT_SERVICE_WITH_APM"], "false")
            self.assertNotIn("volumes", service)
        self.assertEqual(d.read_json(self.app.out / "deployment-status.json")["status"], "application_rollback_completed")

    def test_failed_database_recovery_does_not_recreate_apps(self):
        self.baseline()
        with patch.object(self.flow, "recreate") as recreate, \
                patch.object(self.flow, "wait_database", side_effect=d.Failure("DB unavailable")):
            with self.assertRaisesRegex(d.Failure, "DB unavailable"):
                self.recover()
        recreate.assert_called_once_with("db")

    def test_foreign_container_and_mount_drift_refused(self):
        self.baseline()
        self.report["selected"]["mysql"]["project"] = "production-other"
        with patch.object(self.flow, "recreate") as recreate:
            with self.assertRaisesRegex(d.Failure, "another deployment"):
                self.recover()
        recreate.assert_not_called()
        self.report["selected"]["mysql"]["project"] = self.flow.project
        self.report["selected"]["mysql"]["mounts"][0]["Name"] = "wrong-data"
        with self.assertRaisesRegex(d.Failure, "mount differs"):
            self.recover()

    def test_changed_compose_and_missing_images_refused(self):
        self.baseline()
        self.model["services"]["web"]["image"] = "new-release"
        with self.assertRaisesRegex(d.Failure, "Compose/env changed"):
            self.recover()
        del self.model["services"]["web"]["image"]
        self.image = "sha256:" + "b" * 64
        with self.assertRaisesRegex(d.Failure, "image unavailable"):
            self.recover()

    def test_missing_volume_blocks_before_any_recreation(self):
        self.baseline()
        original = self.docker
        def missing(*args, **kwargs):
            if args[:2] == ("volume", "inspect"):
                raise d.Failure("volume missing")
            return original(*args, **kwargs)
        with patch.object(self, "docker", side_effect=missing), patch.object(self.flow, "recreate") as recreate:
            with self.assertRaisesRegex(d.Failure, "volume missing"):
                self.recover()
        recreate.assert_not_called()

    def test_remote_docker_endpoint_blocks_recovery(self):
        self.baseline()
        with patch.dict(d.os.environ, {"DOCKER_HOST": "tcp://remote:2375", "DOCKER_CONTEXT": ""}), \
                patch.object(self.flow, "recreate") as recreate:
            with self.assertRaisesRegex(d.Failure, "local Docker"):
                self.recover()
        recreate.assert_not_called()

    def test_explicit_remote_context_cannot_be_masked_by_local_host_variable(self):
        with patch.dict(d.os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": "remote"}):
            self.assertEqual(d.docker_endpoint("ssh://remote"), "ssh://remote")
        with patch.dict(d.os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""}):
            self.assertEqual(d.docker_endpoint("unix:///var/run/docker.sock"), "unix:///var/run/docker.sock")

    def test_rollback_requires_maintenance_before_reading_files(self):
        with patch.object(p.sys, "platform", "linux"), patch.object(p.os, "geteuid", return_value=0, create=True), \
                patch.object(p, "Production") as coordinator:
            self.assertEqual(p.main(["rollback"]), 1)
        coordinator.assert_not_called()

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI required for Compose parser check")
    def test_real_compose_parser_merges_generated_recovery_without_instrumentation_mounts(self):
        if subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=15).returncode:
            self.skipTest("Compose plugin unavailable")
        for service in self.model["services"].values():
            service["image"] = "original:latest"
        self.model["services"]["web"]["build"] = {"context": "."}
        self.compose_file.write_text(json.dumps(self.model))
        self.baseline()
        with patch.object(self.flow, "recreate"), patch.object(self.flow, "wait_database"):
            self.recover()
        result = subprocess.run(["docker", "compose", "--project-directory", str(self.root),
            "-p", self.flow.project, "-f", str(self.compose_file), "-f", str(self.flow.override),
            "config", "--format", "json"], capture_output=True, check=True, text=True, timeout=30)
        merged = json.loads(result.stdout)
        for name, service in merged["services"].items():
            self.assertEqual(service["image"], self.image)
            self.assertEqual(service["runtime"], "runc")
            self.assertEqual(service["environment"]["DD_TRACE_ENABLED"], "false")
            self.assertEqual({mount["target"] for mount in service["volumes"]}, set(self.saved["mounts"][name]))
