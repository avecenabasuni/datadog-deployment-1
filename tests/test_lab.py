"""Offline lab generation and environment routing tests; no Docker access."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from test_deploy import config, d, report

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lab_prepare', ROOT / 'lab/prepare.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class LabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        shutil.copyfile(ROOT / 'config/production.example.json', self.root / 'config/production.example.json')

    def test_prepare_no_secret_output_and_no_rotation(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            lab.prepare(self.root)
        secret_file = self.root / 'config/lab-secrets.json'
        secret = json.loads(secret_file.read_text())
        env_file = self.root / 'lab/.env'
        env = dict(line.split('=', 1) for line in env_file.read_text().splitlines())
        self.assertEqual(secret['admin_password'], env['LAB_ROOT_PASSWORD'])
        self.assertNotEqual(secret['db_password'], env['LAB_DB_PASSWORD'])
        self.assertNotIn(secret['admin_password'], output.getvalue())
        before = secret_file.read_bytes()
        with self.assertRaises(lab.deployment.Failure):
            lab.prepare(self.root)
        self.assertEqual(before, secret_file.read_bytes())
        c = json.loads((self.root / 'config/lab.json').read_text())
        self.assertEqual(c['env'], 'lab')
        self.assertEqual(c['mysql_host'], 'eminerba-db')
        if os.name != 'nt':
            self.assertEqual(secret_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_prepare_dry_run(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        lab.prepare(self.root, dry=True)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))

    def test_prepare_partial_state_refused(self):
        (self.root / 'config/lab.json').write_text('{}')
        with self.assertRaises(lab.deployment.Failure):
            lab.prepare(self.root)
        self.assertFalse((self.root / 'lab/.env').exists())

    def test_environment_consistent_across_outputs(self):
        for environment in ('prod', 'lab', 'staging-1'):
            c = config()
            c['env'] = environment
            d.validate(c, full=True)
            c['output_dir'] = str(self.root / environment)
            app = d.Deployment(c, {'api_key': 'test-key', 'db_password': 'test-password'})
            app.render(report())
            out = Path(c['output_dir'])
            override = json.loads((out / 'application.override.json').read_text())
            for role in ('web', 'api'):
                service = override['services']['service-' + role]
                self.assertEqual(service['environment']['DD_ENV'], environment)
                self.assertEqual(service['labels']['com.datadoghq.tags.env'], environment)
            self.assertEqual(app.agent_spec()['environment']['DD_ENV'], environment)
            mysql = json.loads((out / 'mysql.d/conf.yaml').read_text())
            self.assertIn('env:' + environment, mysql['instances'][0]['tags'])

    def test_lab_fixture_safety(self):
        compose = (ROOT / 'lab/docker-compose.yml').read_text()
        self.assertIn('127.0.0.1:8080:80', compose)
        self.assertNotIn('3306:3306', compose)
        self.assertIn('mysql-data:/var/lib/mysql', compose)
        dockerfile = (ROOT / 'lab/Dockerfile').read_text()
        self.assertNotIn('ddtrace', dockerfile)
        self.assertNotIn('datadog-setup', dockerfile)
        self.assertIn('.env', (ROOT / 'lab/.dockerignore').read_text())
        self.assertNotIn('datadog-rum', (ROOT / 'lab/web/index.php').read_text())


if __name__ == '__main__':
    unittest.main()
