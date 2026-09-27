"""CLI formatting, useful failure diagnostics and credential-redaction tests."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from test_deploy import d, config, FakeRunner
import cli_output as cli


class CliOutputTests(unittest.TestCase):
    def test_missing_docker_object_suggests_lab_preparation(self):
        error = cli.command_error(["docker", "inspect", "eminerba_web"],
                                  subprocess.CompletedProcess([], 1, "", "error: no such object: eminerba_web"))
        self.assertIn("docker ps -a", error.hint)
        self.assertIn("eminerba-lab prepare", error.hint)

    def test_known_encoded_and_terminal_colored_secrets_are_redacted(self):
        secret = "test-password' with spaces"
        redactor = cli.Redactor([secret, "abcdef12345"])
        output = redactor.clean(secret + " " + quote(secret, safe="") + " abc\x1b[31mdef12345")
        self.assertNotIn(secret, output)
        self.assertNotIn(quote(secret, safe=""), output)
        self.assertNotIn("abcdef12345", output)
        self.assertNotIn("\x1b", output)

    def test_unregistered_common_credential_formats_are_redacted(self):
        samples = [
            'MYSQL_PASSWORD="unknown secret"', 'DD_API_KEY=unknown-secret',
            '{"clientToken": "unknown-secret"}', 'Authorization: Bearer unknown-secret',
            'Cookie: session=unknown-secret', 'Bearer unknown-secret',
            "CREATE USER x IDENTIFIED BY 'unknown secret';",
            'CREATE USER x IDENTIFIED BY "unknown secret";',
            'https://name:unknown-secret@example.test/path?token=unknown-secret',
            'api_key: |\n  unknown-secret\n  another-line\n',
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                output = cli.Redactor().clean(sample)
                self.assertNotIn("unknown-secret", output)
                self.assertNotIn("unknown secret", output)
                self.assertNotIn("another-line", output)

    def test_runner_surfaces_stderr_and_exit_code_without_secret(self):
        runner = d.Runner()
        runner.redactor.add(["sensitive-value"])
        script = "import sys; print('ERROR 1045: Access denied for user; password=sensitive-value',file=sys.stderr); sys.exit(3)"
        with self.assertRaises(d.Failure) as caught:
            runner.run([sys.executable, "-c", script])
        error = caught.exception
        self.assertEqual(error.exit_code, 3)
        self.assertIn("ERROR 1045", error.details)
        self.assertNotIn("sensitive-value", error.details)
        self.assertIn("MySQL", error.hint)
        self.assertNotIn(script, error.command)

    def test_compose_config_stdout_is_never_used_as_diagnostic(self):
        result = subprocess.CompletedProcess([], 1, 'ERROR unknown-secret\n{"environment": {"PASSWORD":"secret"}}', "")
        error = cli.command_error(["docker", "compose", "-f", "base.yml", "config", "--format", "json"], result)
        self.assertNotIn("unknown-secret", error.details)
        self.assertNotIn("environment", error.details)
        self.assertEqual(error.command, "docker compose config")

    def test_command_summary_hides_shell_sql_environment_and_url_arguments(self):
        args = ["docker", "exec", "-i", "-u", "0", "-w", "/work", "eminerba_db", "sh", "-c", "private shell code"]
        self.assertEqual(cli.command_label(args), "docker exec eminerba_db")
        self.assertEqual(cli.command_label(["curl", "https://user:secret@example.test"]), "curl")

    def test_error_format_has_stage_details_and_next_action(self):
        error = cli.command_error(["docker", "info"], subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon"))
        with contextlib.redirect_stderr(io.StringIO()) as stderr, contextlib.redirect_stdout(io.StringIO()) as stdout:
            cli.report_error(error, "Production | preflight")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("[ERROR] Production | preflight", stderr.getvalue())
        self.assertIn("exit code: 1", stderr.getvalue())
        self.assertIn("[DETAIL] Cannot connect", stderr.getvalue())
        self.assertIn("sudo systemctl status docker", stderr.getvalue())
        self.assertIn("[STOP ]", stderr.getvalue())

    def test_wrapped_apache_failure_retains_underlying_syntax_error(self):
        cause = cli.command_error(["docker", "exec", "web", "apache2ctl", "-t"],
                                  subprocess.CompletedProcess([], 1, "", "Syntax error on line 19 of apache.conf"))
        error = cli.Failure("Apache configuration test failed; restore the saved backup.")
        error.__cause__ = cause
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            cli.report_error(error, "RUM")
        self.assertIn("line 19", stderr.getvalue())

    def test_invalid_json_reports_path_and_location_without_file_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"password":"private",\n}', encoding="utf-8")
            with self.assertRaises(d.Failure) as caught:
                d.read_json(path)
            self.assertIn(str(path), str(caught.exception))
            self.assertIn("Line 2", caught.exception.details)
            self.assertNotIn("private", caught.exception.details)

    def test_readiness_error_keeps_last_diagnostic(self):
        app = d.Deployment(config(), runner=FakeRunner())
        with patch.object(app.r, "run", return_value=subprocess.CompletedProcess([], 7, "", "Failed to connect to server")), \
                patch.object(d.time, "monotonic", side_effect=[0, 0, 300]):
            with self.assertRaises(d.Failure) as caught:
                app.wait_probe(["curl", "https://example.test"], "web")
        self.assertIn("Failed to connect", caught.exception.details)
        self.assertEqual(caught.exception.exit_code, 7)

    def test_discover_json_stdout_is_not_polluted_by_progress(self):
        args = type("Args", (), dict(config="dummy", secrets="absent", dry_run=False, stage="discover"))()
        with patch.object(d, "read_json", return_value=config()), \
                patch.object(d.Deployment, "discover", return_value={"containers": []}), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            d.run_stage(args)
        self.assertEqual(json.loads(stdout.getvalue()), {"containers": []})
        self.assertIn("[STEP ]", stderr.getvalue())

    def test_diagnostic_length_is_bounded(self):
        error = cli.command_error(["docker", "build"], subprocess.CompletedProcess([], 1, "", "failed\n" * 10000))
        self.assertLessEqual(len(error.details), 4000)
        self.assertLessEqual(len(error.details.splitlines()), 12)


if __name__ == "__main__":
    unittest.main()
