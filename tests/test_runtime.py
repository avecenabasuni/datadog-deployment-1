"""Lock, process cleanup and readiness regressions; POSIX cases run real processes."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_deploy import d, config, FakeRunner, ROOT
from test_production import p
from test_rehearsal import lab


class RuntimeTests(unittest.TestCase):
    def test_all_deployment_entry_points_obey_same_lock(self):
        for invoke in (lambda: d.main(["render"]),
                       lambda: p.main(["apply", "--maintenance"]),
                       lambda: p.main(["rollback", "--maintenance"]),
                       lambda: p.main(["offboard", "--maintenance"]),
                       lambda: lab.main(["prepare"]),
                       lambda: lab.main(["repair", "--maintenance"])):
            with patch.object(d, "deployment_lock", side_effect=d.Failure("another run")) as lock, \
                    patch.object(p.sys, "platform", "linux"), \
                    patch.object(p.os, "geteuid", return_value=0, create=True), \
                    patch.object(d, "read_json") as read, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(invoke(), 1)
                lock.assert_called_once_with()
                read.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX flock integration")
    def test_lock_excludes_another_process_and_releases_after_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deployment.lock"
            code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                    "import datadog_deploy as d\n"
                    "try:\n with d.deployment_lock(Path(sys.argv[2])): pass\n"
                    "except d.Failure: sys.exit(7)\n")
            command = [sys.executable, "-c", code, str(ROOT / "scripts"), str(path)]
            with self.assertRaisesRegex(d.Failure, "test failure"):
                with d.deployment_lock(path):
                    self.assertEqual(subprocess.run(command, capture_output=True, timeout=5).returncode, 7)
                    raise d.Failure("test failure")
            self.assertEqual(subprocess.run(command, capture_output=True, timeout=5).returncode, 0)
            self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX signal handling")
    def test_sigterm_interrupts_and_releases_the_deployment_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deployment.lock"
            previous = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(KeyboardInterrupt):
                with d.deployment_lock(path):
                    os.kill(os.getpid(), signal.SIGTERM)
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
            with d.deployment_lock(path):
                pass

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group integration")
    def test_timeout_kills_child_even_when_parent_exits_and_child_ignores_term(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "child.pid"
            child = ("import os,signal,sys,time; from pathlib import Path; "
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                     "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
            parent = ("import subprocess,sys,time; "
                      "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]], "
                      "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(60)")
            with self.assertRaises(d.Failure):
                d.Runner().run([sys.executable, "-c", parent, child, str(pidfile)], timeout=1)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text())
            statfile = Path("/proc") / str(pid) / "stat"
            try:
                deadline = time.monotonic() + 3
                while statfile.exists() and statfile.read_text().split()[2] != "Z" and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(not statfile.exists() or statfile.read_text().split()[2] == "Z")
            finally:
                if statfile.exists() and statfile.read_text().split()[2] != "Z":
                    os.kill(pid, signal.SIGKILL)

    @unittest.skipUnless(os.name == "posix", "POSIX signal integration")
    def test_interrupt_cleans_up_running_command(self):
        command = [sys.executable, "-c",
                   "import os,signal,time; time.sleep(.1); os.kill(os.getppid(),signal.SIGINT); time.sleep(60)"]
        with self.assertRaises(KeyboardInterrupt):
            d.Runner().run(command, timeout=5)

    @unittest.skipUnless(os.name == "posix" and shutil.which("flock") and shutil.which("timeout") and shutil.which("setsid"),
                         "GNU timeout and flock integration")
    def test_container_style_timeout_keeps_mutex_until_job_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = str(Path(folder) / "installer.lock")
            ready = Path(folder) / "ready"
            command = ["flock", "--nonblock", lock, "sh", "-c", d.RUM_SUPERVISOR, "sh", "1s", sys.executable,
                       "-c", "from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(60)", str(ready)]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists())
                self.assertNotEqual(subprocess.run(["flock", "--nonblock", lock, "true"], timeout=2).returncode, 0)
                self.assertIn(process.wait(timeout=3), (124, 137))
                self.assertEqual(subprocess.run(["flock", "--nonblock", lock, "true"], timeout=2).returncode, 0)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("setsid") and shutil.which("timeout"),
                         "Linux container supervisor integration")
    def test_container_supervisor_cleans_up_stubborn_descendant_after_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "child.pid"
            child = ("import os,signal,sys,time; from pathlib import Path; "
                     "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                     "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
            parent = ("import subprocess,sys,time; "
                      "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]], "
                      "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(60)")
            result = subprocess.run(["sh", "-c", d.RUM_SUPERVISOR, "sh", "1s", sys.executable,
                                     "-c", parent, child, str(pidfile)], capture_output=True, timeout=4)
            self.assertEqual(result.returncode, 124)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text())
            statfile = Path("/proc") / str(pid) / "stat"
            try:
                deadline = time.monotonic() + 2
                while statfile.exists() and statfile.read_text().split()[2] != "Z" and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(not statfile.exists() or statfile.read_text().split()[2] == "Z")
            finally:
                if statfile.exists() and statfile.read_text().split()[2] != "Z":
                    os.kill(pid, signal.SIGKILL)

    def test_readiness_retries_connection_failure_and_transient_http_error(self):
        app = d.Deployment(config(), runner=FakeRunner())
        results = [subprocess.CompletedProcess([], code, "", "private-output") for code in (7, 22, 0)]
        with patch.object(app.r, "run", side_effect=results) as run, patch.object(d.time, "sleep") as sleep:
            app.wait_probe(["curl", "private-url"], "web HTTP response")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] <= 10 for call in run.call_args_list))

    def test_readiness_timeout_is_bounded_and_does_not_expose_url_or_output(self):
        app = d.Deployment(config(), runner=FakeRunner())
        with patch.object(app.r, "run", return_value=subprocess.CompletedProcess([], 22, "private", "private")), \
                patch.object(d.time, "monotonic", side_effect=[0, 0, 300]), patch.object(d.time, "sleep") as sleep:
            with self.assertRaisesRegex(d.Failure, "Readiness timed out: web") as error:
                app.wait_probe(["curl", "private-url"], "web")
        self.assertNotIn("private", str(error.exception))
        sleep.assert_not_called()

    def test_application_readiness_does_not_require_curl_in_original_image(self):
        app = d.Deployment(config(), runner=FakeRunner())
        app.wait_application("api")
        args = app.r.calls[0][0]
        self.assertEqual(args[:4], ["docker", "exec", app.c["api_container"], "php"])

    def test_http_readiness_is_read_only_and_checks_both_configured_endpoints(self):
        app = d.Deployment(config(), runner=FakeRunner())
        app.wait_http()
        commands = [args for args, _ in app.r.calls]
        self.assertEqual([args[-1] for args in commands], [app.c["apm_url"], app.c["rum_url"]])
        self.assertTrue(all("--fail" in args and "--output" in args for args in commands))

    def test_http_readiness_skips_missing_empty_and_null_urls_independently(self):
        for urls in ({}, {"rum_url": "", "apm_url": ""},
                     {"rum_url": None, "apm_url": None},
                     {"rum_url": "https://web.example/"},
                     {"apm_url": "https://api.example/read-only"}):
            with self.subTest(urls=urls):
                c = config()
                c.pop("rum_url")
                c.pop("apm_url")
                c.update(urls)
                app = d.Deployment(c, runner=FakeRunner())
                with patch.object(d, "log") as log:
                    app.wait_http()
                expected = [c[key] for key in ("apm_url", "rum_url") if c.get(key)]
                self.assertEqual([args[-1] for args, _ in app.r.calls], expected)
                messages = "\n".join(call.args[0] for call in log.call_args_list)
                for key in ("apm_url", "rum_url"):
                    if not c.get(key):
                        self.assertIn("Skipping " + key + " HTTP readiness", messages)


if __name__ == "__main__":
    unittest.main()
