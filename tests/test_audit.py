"""Regression coverage for lifecycle and diagnostic findings from the final audit."""
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test_deploy import d
import test_production as production
from test_production import p
import cli_output as cli


class AuditTests(unittest.TestCase):
    setUp = production.ProductionTests.setUp

    def test_failed_and_interrupted_status_is_written_before_lock_release(self):
        for error, expected in ((d.Failure("deployment failed"), "failed"),
                                (KeyboardInterrupt(), "interrupted")):
            events = []

            @contextlib.contextmanager
            def lock():
                events.append("locked")
                try:
                    yield
                finally:
                    events.append("released")

            self.flow.journal_enabled = True
            with patch.object(d, "deployment_lock", lock), \
                    patch.object(p.sys, "platform", "linux"), \
                    patch.object(p.os, "geteuid", return_value=0, create=True), \
                    patch.object(d, "read_json", return_value={}), \
                    patch.object(d, "Deployment"), patch.object(p, "Production", return_value=self.flow), \
                    patch.object(self.flow, "run", side_effect=error), \
                    patch.object(self.flow, "record_status", side_effect=lambda status: events.append(status)), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(p.main(["apply", "--maintenance"]), 1)
            self.assertEqual(events, ["locked", expected, "released"])

    def test_journal_write_failure_does_not_hide_deployment_error(self):
        self.flow.journal_enabled = True
        output = io.StringIO()
        with patch.object(self.flow, "record_status", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(d.Failure, "original failure"):
                with self.flow.track_failure():
                    raise d.Failure("original failure")
        self.assertIn("Could not update deployment-status.json", output.getvalue())

    def test_runner_preserves_non_utf8_failure_diagnostics(self):
        with self.assertRaises(d.Failure) as caught:
            d.Runner().run([sys.executable, "-c",
                           "import os; os.write(2,b'ERROR invalid byte: \\xff'); raise SystemExit(9)"])
        self.assertEqual(caught.exception.exit_code, 9)
        self.assertIn("ERROR invalid byte", caught.exception.details)

    def test_working_directory_is_visible_but_mysql_password_is_redacted(self):
        runner = d.Runner()
        with patch.object(cli, "_redactor", cli.Redactor()), \
                patch.dict(d.os.environ, {"PWD": "/useful/current/path", "OLDPWD": "/useful/previous/path",
                                          "MYSQL_PWD": "unique-audit-password"}):
            runner.run([sys.executable, "-c", "pass"])
            output = runner.redactor.clean("/useful/current/path /useful/previous/path unique-audit-password")
        self.assertIn("/useful/current/path", output)
        self.assertIn("/useful/previous/path", output)
        self.assertNotIn("unique-audit-password", output)

    def test_database_probe_uses_remaining_deadline_and_keeps_failure(self):
        previous = d.Failure("Command failed", details="Access denied for user", exit_code=1)
        with patch.object(self.app, "mysql", side_effect=previous) as mysql, \
                patch.object(p.time, "monotonic", side_effect=[0, 299, 300]):
            with self.assertRaises(d.Failure) as caught:
                self.flow.wait_database()
        mysql.assert_called_once_with("SELECT 1;", timeout=1)
        self.assertEqual(caught.exception.details, previous.details)

    def test_agent_probe_uses_remaining_deadline_and_keeps_failure(self):
        timeout = self.app.c["timeout"]
        with patch.object(self.app.r, "run", return_value=subprocess.CompletedProcess([], 1, "", "ERROR unhealthy")) as run, \
                patch.object(d.time, "monotonic", side_effect=[0, timeout - 0.5, timeout]):
            with self.assertRaises(d.Failure) as caught:
                self.app.wait_agent()
        self.assertEqual(run.call_args.kwargs["timeout"], 0.5)
        self.assertIn("unhealthy", caught.exception.details)

    def test_missing_bind_source_blocks_recreation(self):
        self.flow.validate_stack(self.report, self.model)
        self.flow.base_digest = p.digest(self.model)
        # Remove only the empty synthetic directory created by this test fixture.
        Path(self.root / "apiminerba").rmdir()
        with patch.object(self.flow, "model", return_value=self.model):
            with self.assertRaisesRegex(d.Failure, "refusing empty replacement"):
                self.flow.recreate("api")
        self.assertFalse(any("up" in args for args, _ in self.app.r.calls))

    def test_missing_volume_blocks_recreation(self):
        self.flow.validate_stack(self.report, self.model)
        self.flow.base_digest = p.digest(self.model)
        with patch.object(self.flow, "model", return_value=self.model), \
                patch.object(self.app, "docker", side_effect=d.Failure("No such volume")):
            with self.assertRaisesRegex(d.Failure, "No such volume"):
                self.flow.recreate("db")
        self.assertFalse(any("up" in args for args, _ in self.app.r.calls))


if __name__ == "__main__":
    unittest.main()
