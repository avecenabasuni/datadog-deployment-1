#!/usr/bin/env python3
"""Build a disposable production-layout lab, then use the production coordinator."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time

import datadog_deploy as d
import eminerba_production as p

STACK = Path("/opt/eminerba-rehearsal/stack")
CONFIG = d.ROOT / "config/eminerba-rehearsal.json"
SECRETS = d.ROOT / "config/eminerba-rehearsal-secrets.json"


def fixture_model():
    build = {"context": ".", "dockerfile": "docker/DockerFile",
             "args": {"PHP_IMAGE": "${PHP_IMAGE:?Missing PHP_IMAGE}"}}
    common = {"build": build, "restart": "unless-stopped", "networks": ["eminerba_network"]}
    db_env = {"DB_HOST": "db", "DB_USER": "${MYSQL_USER}", "DB_NAME": "${MYSQL_DATABASE}",
              "DB_NAME2": "${MYSQL_DATABASE2}"}
    return {"version": "3.8", "services": {
        "web": dict(common, container_name="eminerba_web", image="eminerba-rehearsal-web:php81",
                    ports=["127.0.0.1:8081:80"],
                    volumes=["./pit_to_port:/var/www/html", "php_sessions:/var/lib/php/sessions"],
                    environment=dict(db_env, DB_PASS="${MYSQL_PASSWORD}"),
                    depends_on={"db": {"condition": "service_healthy"}}),
        "api": dict(common, container_name="eminerba_api", image="eminerba-rehearsal-api:php81",
                    ports=["127.0.0.1:8080:80"], volumes=["./apiminerba:/var/www/html"],
                    environment=dict(db_env, DB_PASSWORD="${MYSQL_PASSWORD}"),
                    depends_on={"db": {"condition": "service_healthy"}}),
        "db": {"image": "${MYSQL_IMAGE:?Missing MYSQL_IMAGE}", "container_name": "eminerba_db",
               "restart": "unless-stopped", "env_file": [".env"], "networks": ["eminerba_network"],
               "volumes": ["mysql_data:/var/lib/mysql", "./docker/my.cnf:/etc/mysql/conf.d/my.cnf",
                           "./docker/001-seed.sql:/docker-entrypoint-initdb.d/001-seed.sql:ro"],
               "healthcheck": {"test": ["CMD-SHELL", 'MYSQL_PWD="$${MYSQL_PASSWORD}" mysql --protocol=TCP -h 127.0.0.1 -u eminerba_lab eminerba_lab -Nse "SELECT COUNT(*) FROM samples" >/dev/null'],
                               "interval": "10s", "timeout": "5s", "retries": 30, "start_period": "30s"}}},
        "volumes": {"mysql_data": {}, "vendor_data": {}, "php_sessions": {}},
        "networks": {"eminerba_network": {"driver": "bridge"}}}


def create_fixture(stack):
    stack = Path(stack)
    d.need(not stack.is_symlink(), "Lab directory must not be a symlink.")
    if stack.exists() and any(stack.iterdir()):
        d.need((stack / ".eminerba-lab.json").is_file(),
               "Nonempty destination is not a generated lab; refusing to overwrite.")
        p.validate_lab_fixture(stack / "docker-compose.yml")
        print("Existing fixture preserved; no password rotation.")
        return
    stack.mkdir(parents=True, exist_ok=True, mode=0o755)
    root_password, app_password = secrets.token_hex(24), secrets.token_hex(24)
    body = ("PHP_IMAGE=php:8.1-apache-bookworm\nMYSQL_IMAGE=mysql:8.0\n"
            "MYSQL_DATABASE=eminerba_lab\nMYSQL_DATABASE2=eminerba_lab_aux\nMYSQL_USER=eminerba_lab\n"
            "MYSQL_ROOT_PASSWORD=" + root_password + "\nMYSQL_PASSWORD=" + app_password + "\n")
    d.secure_write(stack / ".env", body)
    files = {"docker-compose.yml": d.jdump(fixture_model()),
             ".dockerignore": ".env\n.eminerba-lab.json\n",
             "pit_to_port/index.php": (d.ROOT / "lab/web/index.php").read_text(encoding="utf-8"),
             "apiminerba/index.php": (d.ROOT / "lab/api/index.php").read_text(encoding="utf-8")}
    for name in ("DockerFile", "apache.conf", "php.ini", "my.cnf"):
        files["docker/" + name] = (d.ROOT / "lab/production" / name).read_text(encoding="utf-8")
    for folder in ("pit_to_port", "apiminerba"):
        files[folder + "/health.php"] = (d.ROOT / "lab/common/health.php").read_text(encoding="utf-8")
    files["docker/001-seed.sql"] = ((d.ROOT / "lab/mysql/001-seed.sql").read_text(encoding="utf-8") +
        "\nCREATE DATABASE eminerba_lab_aux;\n"
        "CREATE TABLE eminerba_lab_aux.samples LIKE eminerba_lab.samples;\n"
        "INSERT INTO eminerba_lab_aux.samples SELECT * FROM eminerba_lab.samples;\n"
        "GRANT SELECT ON eminerba_lab_aux.* TO 'eminerba_lab'@'%';\n")
    for relative, content in files.items():
        path = stack / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        d.secure_write(path, content)
        path.chmod(0o644)
    marker = {"kind": p.LAB_KIND, "project": p.LAB_PROJECT,
              "hashes": {name: hashlib.sha256((stack / name).read_bytes()).hexdigest()
                         for name in ("docker-compose.yml", ".env")}}
    d.secure_write(stack / ".eminerba-lab.json", d.jdump(marker))
    print("Created dummy production-layout fixture: " + str(stack))


def fill_generated_admin_password(stack, secret_path):
    credentials = d.read_json(secret_path, secret=True)
    if credentials.get("admin_password") == "":
        env = dict(line.split("=", 1) for line in (stack / ".env").read_text().splitlines() if "=" in line)
        credentials["admin_password"] = env["MYSQL_ROOT_PASSWORD"]
        d.secure_write(secret_path, d.jdump(credentials))


def repair(stack=STACK, config_path=CONFIG, secret_path=SECRETS, dry=False):
    """Repair only the auxiliary dummy schema on a verified rehearsal database."""
    p.validate_lab_fixture(stack / "docker-compose.yml")
    c = d.read_json(config_path)
    d.need(c.get("env") == "lab" and c.get("mysql_container") == "eminerba_db" and
           c.get("mysql_port") == 3306 and c.get("admin_user") == "root" and
           set(c.get("mysql_schemas", [])) == {"eminerba_lab", "eminerba_lab_aux"},
           "Repair supports only the generated dummy lab, not production/custom schemas.")
    app = d.Deployment(c, d.read_json(secret_path, secret=True))
    endpoint = app.docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    d.need(os.environ.get("DOCKER_HOST", endpoint) == "unix:///var/run/docker.sock", "Repair requires the local lab Docker socket.")
    security = json.loads(app.docker("info", "--format", "{{json .SecurityOptions}}")) or []
    d.need(not any("rootless" in value for value in security), "Repair requires rootful Docker.")
    item = app.inspect("eminerba_db")
    d.need(item["project"] == p.LAB_PROJECT and item["service"] == "db" and item["running"],
           "Database is not the running rehearsal service; refusing repair.")
    sources = json.loads(app.docker("inspect", "--format",
        '{{json (index .Config.Labels "com.docker.compose.project.config_files")}}', "eminerba_db"))
    files = {str(Path(value).resolve()) for value in (sources or "").split(",")}
    base = str((stack / "docker-compose.yml").resolve())
    d.need(base in files and files <= {base, str((stack.parent / "generated/production.override.json").resolve())},
           "Database Compose source differs from the generated fixture.")
    d.need(any(m["Destination"] == "/var/lib/mysql" and m["Type"] == "volume" and
               m.get("Name") == p.LAB_PROJECT + "_mysql_data" and m.get("RW") for m in item["mounts"]),
           "Database does not use the rehearsal data volume; refusing repair.")
    d.need(app.mysql("SELECT @@datadir;").rstrip("/") == "/var/lib/mysql", "Unexpected repair datadir.")
    d.need(app.mysql("SELECT COUNT(*) FROM eminerba_lab.samples;") == "3",
           "Primary dummy samples differ; inspect fixture data before repair.")
    if dry:
        print("PLAN: ensure eminerba_lab_aux.samples, copy missing dummy rows, and grant lab SELECT access. No SQL changes.")
        return
    app.mysql("CREATE DATABASE IF NOT EXISTS eminerba_lab_aux;\n"
              "CREATE TABLE IF NOT EXISTS eminerba_lab_aux.samples LIKE eminerba_lab.samples;\n"
              "INSERT INTO eminerba_lab_aux.samples (id,name,status) "
              "SELECT s.id,s.name,s.status FROM eminerba_lab.samples s "
              "LEFT JOIN eminerba_lab_aux.samples a ON a.id=s.id WHERE a.id IS NULL;\n"
              "GRANT SELECT ON eminerba_lab_aux.* TO 'eminerba_lab'@'%';\n")
    app.check_mysql_schemas()
    d.need(app.mysql("SELECT COUNT(*) FROM eminerba_lab_aux.samples WHERE id IN (1,2,3);") == "3",
           "Auxiliary dummy rows still incomplete; inspect fixture before retrying.")
    print("Lab auxiliary schema ready. Existing rows/passwords/volumes were preserved.")


def prepare(stack=STACK, config_path=CONFIG, secret_path=SECRETS, runner=None):
    runner = runner or d.Runner(60)
    def run(args, **kwargs):
        return runner.run(args, **kwargs).stdout.strip()
    info = json.loads(run(["docker", "info", "--format", "{{json .}}"] ))
    d.need(not any("rootless" in value for value in info.get("SecurityOptions", [])), "Lab needs rootful Docker.")
    endpoint = run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
    d.need(os.environ.get("DOCKER_HOST", endpoint) == "unix:///var/run/docker.sock", "Use this VM's local Docker socket.")
    names = set(run(["docker", "ps", "-a", "--format", "{{.Names}}"] ).splitlines())
    allowed = {container for _, container in p.MAPPING.values()} | {p.LAB_PROJECT + "-agent"}
    d.need(not names - allowed,
           "Other containers/old lab detected. Use a dedicated clean VM; this command will not remove them.")
    d.need(runner.run(["systemctl", "is-active", "datadog-agent"], check=False).returncode != 0,
           "Existing host Datadog Agent detected; use a clean lab VM.")
    # A production container with the same name must never be taken over.
    for container in sorted(names):
        if container == p.LAB_PROJECT + "-agent":
            managed = json.loads(run(["docker", "inspect", "--format",
                                      '{{json (index .Config.Labels "id.sucofindo.datadog.managed")}}', container]))
            d.need(managed == "true", "The rehearsal Agent name is occupied by an unmanaged container.")
            continue
        project = json.loads(run(["docker", "inspect", "--format",
                                  '{{json (index .Config.Labels "com.docker.compose.project")}}', container]))
        d.need(project == p.LAB_PROJECT, "Container name belongs to another project: " + container)
    create_fixture(stack)
    if config_path.exists() or secret_path.exists():
        d.need(config_path.is_file() and secret_path.is_file(), "Partial lab configuration; preserve files and inspect locally.")
        d.need(d.read_json(config_path).get("env") == "lab", "Existing configuration is not a lab profile.")
        fill_generated_admin_password(stack, secret_path)
        print("Lab configuration already exists. Use eminerba-lab --dry-run or --maintenance; baseline was not recreated.")
        return
    compose = ["bash", str(d.ROOT / "scripts/compose"), "--project-directory", str(stack),
               "--env-file", str(stack / ".env"), "-p", p.LAB_PROJECT, "-f", str(stack / "docker-compose.yml")]
    print("Building and starting the baseline (this can take several minutes).", flush=True)
    run(compose + ["config", "-q"])
    run(compose + ["build", "--pull"], timeout=1800)
    run(compose + ["up", "-d"], timeout=600)
    deadline = time.monotonic() + 300
    while True:
        probe = runner.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                            "http://127.0.0.1:8081/api/"], check=False)
        if probe.returncode == 0:
            payload = json.loads(probe.stdout)
            d.need(payload.get("driver") == "pdo_mysql" and len(payload.get("samples", [])) == 3,
                   "Unexpected baseline response; check that ports belong to this lab.")
            break
        d.need(time.monotonic() < deadline, "Baseline API not ready; inspect eminerba_db/eminerba_api logs locally.")
        time.sleep(3)
    p.prepare(config_path, secret_path, stack / "docker-compose.yml", lab=True)
    # The generated DB root password is known locally; never ask the user to copy it.
    fill_generated_admin_password(stack, secret_path)
    repair(stack, config_path, secret_path)
    print("Baseline ready at http://127.0.0.1:8081/. Fill lab Datadog values and reviewed installer hashes once.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("prepare", "apply", "repair"), default="apply")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--maintenance", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "apply":
        command = ["apply", "--lab"]
        if args.dry_run:
            command.append("--dry-run")
        if args.maintenance:
            command.append("--maintenance")
        return p.main(command)
    try:
        d.need(sys.platform.startswith("linux") and os.geteuid() == 0, "Run on a dedicated Ubuntu VM with sudo.")
        if args.action == "repair":
            d.need(args.dry_run or args.maintenance, "Lab SQL repair requires --maintenance.")
        else:
            d.need(not args.dry_run, "prepare builds the dummy lab; --dry-run applies to deployment or repair.")
        import fcntl
        with open("/run/lock/eminerba-observability.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise d.Failure("Another deployment is active.") from exc
            if args.action == "repair":
                repair(dry=args.dry_run)
            else:
                prepare()
        return 0
    except (d.Failure, OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, d.Failure) else "Invalid lab data; details withheld to protect secrets."
        print("ERROR lab " + args.action + ": " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
