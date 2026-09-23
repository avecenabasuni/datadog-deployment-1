#!/usr/bin/env python3
"""Prepare local lab files only. Does not invoke Docker, install SSI, or deploy."""
import argparse
import importlib.util
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('deployment', ROOT / 'scripts/datadog_deploy.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


def prepare(root=ROOT, dry=False):
    targets = [root / 'lab/.env', root / 'config/lab.json', root / 'config/lab-secrets.json']
    existing = [path for path in targets if path.exists() or path.is_symlink()]
    if existing:
        raise deployment.Failure('Lab files already exist; preserve them and edit locally. No passwords were rotated.')
    if dry:
        print('DRY-RUN: would create lab/.env, config/lab.json, config/lab-secrets.json; no files written.')
        return
    root_password = secrets.token_hex(24)
    application_password = secrets.token_hex(24)
    c = json.loads((root / 'config/production.example.json').read_text(encoding='utf-8'))
    c.update(web_container='eminerba-lab-web', api_container='eminerba-lab-api',
             mysql_container='eminerba-lab-db', network='eminerba-lab-network',
             agent_name='eminerba-lab-agent', env='lab', version='lab-1',
             web_service='eminerba-lab-web', api_service='eminerba-lab-api',
             web_compose_service='eminerba-web', api_compose_service='eminerba-api',
             mysql_compose_service='eminerba-db', mysql_host='eminerba-db',
             mysql_schemas=['eminerba_lab'], db_apps=['api'],
             artifact_dir='/opt/sucofindo-lab/artifacts', output_dir='/opt/sucofindo-lab/generated',
             agent_run_dir='/opt/sucofindo-lab/agent-run',
             rum_url='http://127.0.0.1:8080/', apm_url='http://127.0.0.1:8080/api/',
             rum_application_id='REPLACE_LAB_RUM_APPLICATION_ID',
             rum_client_token='REPLACE_LAB_RUM_CLIENT_TOKEN',
             rum_remote_configuration_id='REPLACE_LAB_RUM_REMOTE_CONFIGURATION_ID')
    # Moving version-family tags are lab starting points, not immutable production pins.
    env = ('COMPOSE_PROJECT_NAME=eminerba-lab\n'
           'PHP_IMAGE=php:8.1-apache-bookworm\nMYSQL_IMAGE=mysql:8.0\n'
           'LAB_ROOT_PASSWORD=' + root_password + '\n'
           'LAB_DB_PASSWORD=' + application_password + '\n')
    credentials = {'api_key': 'REPLACE_NEW_LAB_API_KEY',
                   'db_password': secrets.token_hex(24), 'admin_password': root_password}
    for path, body in zip(targets, [env, deployment.jdump(c), deployment.jdump(credentials)]):
        deployment.secure_write(path, body)
    print('Created private lab configuration. Fill lab Datadog values locally; follow lab/README.md.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        prepare(dry=args.dry_run)
        return 0
    except (deployment.Failure, OSError) as error:
        print(str(error) if isinstance(error, deployment.Failure) else 'Cannot prepare local lab files.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
