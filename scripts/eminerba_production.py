#!/usr/bin/env python3
"""Opt-in lifecycle coordinator for the supplied Eminerba production Compose stack."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

import datadog_deploy as d

MAPPING = {"web": ("web", "eminerba_web"), "api": ("api", "eminerba_api"),
           "mysql": ("db", "eminerba_db")}
RUM_KEYS = ("rum_application_id", "rum_client_token", "rum_remote_configuration_id", "site")


def digest(value):
    return hashlib.sha256(d.jdump(value).encode()).hexdigest()


def mount_source(model, project, mount, directory):
    """Resolve a normalized Compose mount without dumping its environment."""
    if isinstance(mount, str):
        parts = mount.split(":")
        d.need(len(parts) in (2, 3), "Anonymous/unsupported Compose mount; review required.")
        source, target = parts[:2]
        kind = "bind" if source.startswith(("/", ".")) else "volume"
    else:
        kind, source, target = mount["type"], mount.get("source"), mount["target"]
    d.need(source and kind in ("bind", "volume"), "Unsupported Compose mount; review required.")
    if kind == "bind":
        source = str((directory / source).resolve())
    else:
        spec = (model.get("volumes") or {}).get(source) or {}
        external = spec.get("external")
        source = (spec.get("name") or (external.get("name") if isinstance(external, dict) else None)
                  or (source if external else project + "_" + source))
    return target, (kind, source)


class Production:
    def __init__(self, app, compose_file):
        self.app = app
        self.c = app.c
        self.compose_file = Path(compose_file).resolve()
        self.directory = self.compose_file.parent
        self.project = None
        self.backend = None
        self.stage = "initial checks"
        self.override = app.out / "production.override.json"
        self.state_path = app.out / "production-state.json"

    def step(self, name):
        self.stage = name
        print("\n[production] " + name, flush=True)

    def compose(self, *args, overlay=False):
        command = self.backend + ["--project-directory", str(self.directory), "-p", self.project,
                                  "-f", str(self.compose_file)]
        if (self.directory / ".env").is_file():
            command += ["--env-file", str(self.directory / ".env")]
        if overlay:
            command += ["-f", str(self.override)]
        return self.app.run(command + list(args), timeout=1800)

    def model(self, overlay=False):
        if self.backend == ["docker", "compose"]:
            return json.loads(self.compose("config", "--format", "json", overlay=overlay))
        # Legacy Compose has no JSON output. Never print its resolved secrets.
        try:
            import yaml
        except ImportError as exc:
            raise d.Failure("Legacy Compose requires python3-yaml (sudo apt-get install python3-yaml).") from exc
        try:
            return yaml.safe_load(self.compose("config", overlay=overlay))
        except yaml.YAMLError as exc:
            raise d.Failure("Resolved Compose YAML invalid; output withheld to protect secrets.") from exc

    def validate_stack(self, report, model):
        d.need(isinstance(model, dict), "Compose model must be an object.")
        d.need(set(model.get("services", {})) == {"web", "api", "db"},
               "Coordinator supports exactly web/api/db; review additional services first.")
        self.preserved_mounts = {}
        for role, (service, container) in MAPPING.items():
            item = report["selected"][role]
            d.need(self.c[role + "_container"] == container and item["service"] == service,
                   "Production mapping differs for " + role)
            spec = model["services"][service]
            d.need(spec.get("container_name") == container, "Compose container_name differs: " + service)
            expected = dict(mount_source(model, self.project, mount, self.directory)
                            for mount in spec.get("volumes", []))
            self.preserved_mounts[service] = expected
            actual = {m["Destination"]: (m["Type"], m.get("Name") if m["Type"] == "volume" else m["Source"])
                      for m in item["mounts"]}
            for target, source in expected.items():
                d.need(actual.get(target) == source,
                       "Live mount differs from Compose: " + service + " " + target)
            managed = {self.c["socket_dir"], self.c["mysql_config_target"]}
            if self.state.get("rum_override"):
                managed.update((report["apache_root"], "/opt/datadog-httpd"))
            d.need(not set(actual) - set(expected) - managed,
                   "Live container has extra mounts not in the base Compose: " + service)
            if role == "mysql":
                d.need(self.c["mysql_data_dir"] in expected, "Compose must preserve the actual MySQL datadir mount.")
                self.database_mount = expected[self.c["mysql_data_dir"]]
                d.need("/etc/mysql/conf.d/my.cnf" in expected, "Production my.cnf mount missing.")
            else:
                folder = "pit_to_port" if role == "web" else "apiminerba"
                d.need(expected.get("/var/www/html") == ("bind", str(self.directory / folder)),
                       "Unexpected application source mount: " + service)
            networks = spec.get("networks") or {}
            resolved = []
            for key in networks:
                network = (model.get("networks") or {}).get(key) or {}
                external = network.get("external")
                name = (network.get("name") or
                        (external.get("name") if isinstance(external, dict) else None) or
                        (key if external else self.project + "_" + key))
                d.need(name in item["networks"], "Compose network differs from live container: " + service)
                resolved.append(name)
            d.need(self.c["network"] in resolved, "Agent network absent from Compose: " + service)

    def check(self):
        d.need(self.compose_file.is_file(), "Compose file not found.")
        d.need(self.c["env"] == "prod", "This command requires env=prod; use the lab runbook for lab.")
        d.need(self.c["mysql_data_dir"] == "/var/lib/mysql" and
               self.c["mysql_config_target"] == "/etc/mysql/conf.d/zz-datadog.cnf",
               "Production requires /var/lib/mysql and a separate /etc/mysql/conf.d/zz-datadog.cnf.")
        self.app.secret("api_key")
        self.app.secret("db_password")
        self.app.secret("admin_password")
        self.app.artifact("ssi")
        self.app.artifact("rum")
        report = self.app.preflight(full=True, require_rum_tools=False)
        projects = {item["project"] for item in report["selected"].values()}
        d.need(len(projects) == 1 and None not in projects and "" not in projects, "Live Compose project is required.")
        self.project = projects.pop()
        for role, (_, container) in MAPPING.items():
            sources = json.loads(self.app.docker("inspect", "--format",
                '{{json (index .Config.Labels "com.docker.compose.project.config_files")}}', container))
            d.need(sources, "Missing Compose source labels: " + role)
            actual_files = {str(Path(value).resolve()) for value in sources.split(",")}
            d.need(str(self.compose_file) in actual_files and
                   actual_files <= {str(self.compose_file), str(self.override)},
                   "Container uses different/additional Compose files: " + role)
        inv = report["inventory"]
        d.need(not inv["host_agent_active"], "Existing host Agent; resolve ownership before deployment.")
        d.need(not set(inv["agent_candidates"]) - {self.c["agent_name"]},
               "Existing other Agent; this command will not replace it.")
        if inv["agent_candidates"]:
            agent = self.app.inspect(self.c["agent_name"])
            d.need(agent["managed"] == "true" and agent["running"], "Existing Agent is not a running managed Agent.")
        d.need(not inv["ssi_packages"] or inv["default_runtime"] == "dd-shim", "Partial SSI installation; review before retry.")
        if self.app.r.run(["docker", "compose", "version"], check=False).returncode == 0:
            self.backend = ["docker", "compose"]
        else:
            self.app.run(["docker-compose", "version"])
            self.backend = ["docker-compose"]
        self.state = d.read_json(self.state_path, secret=True) if self.state_path.exists() else {}
        model = self.model()
        self.validate_stack(report, model)
        self.check_environment(model)
        datadir = self.app.mysql("SELECT @@datadir;").rstrip("/")
        d.need(datadir == self.c["mysql_data_dir"].rstrip("/"), "Actual MySQL datadir differs; refusing recreation.")
        self.base_digest = digest(model)
        self.base_version = model.get("version")
        if self.state:
            d.need(self.state["compose_digest"] == self.base_digest, "Compose/env changed; review existing production state before rerun.")
            d.need(self.state["rum_digest"] == digest({k: self.c[k] for k in RUM_KEYS}),
                   "RUM settings changed; explicit RUM reconfiguration required.")
        modules = self.app.docker("exec", self.c["web_container"], report["apache"], "-M")
        d.need("datadog_module" not in modules or bool(self.state.get("rum_override")),
               "Existing RUM without coordinator persistence state; review/export it before adoption.")
        return report

    def check_environment(self, model):
        # Compare only keys from the base Compose. Never request Config.Env or
        # put credential values in argv, console output, or persisted reports.
        probe = ('command -v printenv >/dev/null && command -v sha256sum >/dev/null || exit 2; '
                 'printenv "$1" | sha256sum')
        for _, (service, container) in MAPPING.items():
            for key, value in (model["services"][service].get("environment") or {}).items():
                d.need(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and value is not None,
                       "Unresolved/unsupported environment key in " + service)
                expected = hashlib.sha256((str(value) + "\n").encode()).hexdigest()
                actual = self.app.docker("exec", container, "sh", "-c", probe, "sh", key).split()[0]
                d.need(actual == expected, "Compose environment differs from live container: " + service + "/" + key
                       + "; review .env/shell overrides before recreation.")

    def wait_database(self):
        deadline = time.monotonic() + max(300, self.c["timeout"])
        while True:
            try:
                if self.app.mysql("SELECT 1;") == "1":
                    return
            except d.Failure:
                pass
            d.need(time.monotonic() < deadline, "MySQL readiness timed out; preserved volume, inspect DB logs locally.")
            time.sleep(3)

    def recreate(self, service):
        d.need(digest(self.model()) == self.base_digest, "Compose/env changed during deployment; stopping before recreation.")
        self.compose("config", "-q", overlay=True)
        merged = self.model(overlay=True)
        for name, mounts in self.preserved_mounts.items():
            resulting = dict(mount_source(merged, self.project, mount, self.directory)
                             for mount in merged["services"][name].get("volumes", []))
            d.need(all(resulting.get(target) == source for target, source in mounts.items()),
                   "Merged override changes an existing mount: " + name)
        self.compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", service, overlay=True)

    def images(self):
        images = {}
        for role, (service, container) in MAPPING.items():
            image_id = json.loads(self.app.docker("inspect", "--format", "{{json .Image}}", container))
            d.need(re.fullmatch(r"sha256:[a-f0-9]{64}", image_id), "Invalid running image ID.")
            base = "eminerba-observability/" + service + "-base:" + image_id[7:]
            self.app.docker("image", "tag", image_id, base)
            self.state.setdefault("original_images", {}).setdefault(service, base)
            if role == "mysql":
                images[service] = base
                continue
            tools = self.app.r.run(["docker", "exec", container, "sh", "-c",
                                   "command -v curl && command -v tar && command -v gzip && command -v gpg"], check=False)
            if tools.returncode == 0:
                images[service] = base
                continue
            context = self.app.out / ("tools-build-" + service)
            dockerfile = ("FROM " + base + "\nUSER root\n"
                          "RUN apt-get update && apt-get install -y --no-install-recommends "
                          "ca-certificates curl gnupg tar gzip && rm -rf /var/lib/apt/lists/*\n")
            original_user = json.loads(self.app.docker("image", "inspect", "--format", "{{json .Config.User}}", image_id))
            d.need(original_user in ("", "root", "0"), "Derived image expects the supplied root Apache image.")
            d.secure_write(context / "Dockerfile", dockerfile)
            image = "eminerba-observability/" + service + "-tools:" + digest(dockerfile)[:24]
            self.app.docker("build", "--pull=false", "-t", image, str(context), timeout=1800)
            images[service] = image
        return images

    def save_state(self):
        d.secure_write(self.state_path, d.jdump(self.state))

    def run(self, dry=False):
        self.step("preflight and existing project/volume checks")
        report = self.check()
        print("Project: " + self.project + "; services: web, api, db; data volume will be preserved.")
        if dry:
            print("PLAN: render -> prepare pinned images/tools -> Agent -> SSI -> recreate DB -> DBM -> "
                  "recreate API/web -> RUM -> persist RUM -> recreate web -> verify/smoke.")
            print("No deployment changes made. This is not end-to-end acceptance.")
            return
        self.step("render and prepare application images")
        self.app.render(report)
        overlay = d.read_json(self.app.out / "application.override.json")
        if getattr(self, "base_version", None):
            overlay["version"] = self.base_version
        for service, image in self.images().items():
            overlay["services"][service]["image"] = image
            overlay["services"][service]["runtime"] = "dd-shim"
        # Existing persistence must accompany every subsequent web recreation.
        if self.state.get("rum_override"):
            persistent = d.read_json(self.state["rum_override"], secret=True)
            overlay["services"]["web"]["volumes"] += persistent["services"]["web"]["volumes"]
        d.secure_write(self.override, d.jdump(overlay))
        self.compose("config", "-q", overlay=True)
        self.state.update(compose_digest=self.base_digest,
                          rum_digest=digest({k: self.c[k] for k in RUM_KEYS}))
        self.save_state()
        self.step("Agent and host SSI")
        self.app.start_agent(report)
        self.app.install_ssi(report)
        self.step("recreate DB using the same data volume, then provision DBM")
        self.recreate("db")
        self.wait_database()
        actual = self.app.inspect(self.c["mysql_container"])
        actual_mounts = {m["Destination"]: (m["Type"], m.get("Name") if m["Type"] == "volume" else m["Source"])
                         for m in actual["mounts"]}
        d.need(actual_mounts.get(self.c["mysql_data_dir"]) == self.database_mount, "DB mount changed; stop and inspect immediately.")
        self.app.verify_mysql_startup(admin=True)
        self.app.dbm_sql(report)
        self.step("recreate API and web with SSI settings")
        self.recreate("api")
        self.recreate("web")
        report = self.app.preflight(full=True)
        self.step("install RUM and persist Apache/module assets")
        export = self.app.install_rum(report) or self.app.export_rum(report)
        persistence_path = export / "persistence.override.example.json"
        persistent = d.read_json(persistence_path, secret=True)
        new_mounts = persistent["services"]["web"]["volumes"]
        replaced_targets = {mount.rsplit(":", 2)[1] for mount in new_mounts}
        overlay["services"]["web"]["volumes"] = [
            mount for mount in overlay["services"]["web"]["volumes"] if mount.rsplit(":", 2)[1] not in replaced_targets
        ] + new_mounts
        d.secure_write(self.override, d.jdump(overlay))
        self.state["rum_override"] = str(persistence_path)
        self.save_state()
        self.recreate("web")
        self.step("verify after recreation and HTTP smoke")
        self.app.verify(self.app.preflight(full=True))
        self.app.smoke()
        print("LOCAL CHECKS PASSED. Browser RUM, real HTTP PHP tracing, and DBM correlation remain manual acceptance.")
        print("Keep the production override in future Compose operations: " + str(self.override))


def prepare(config_path, secret_path, compose_file):
    d.need(not any(path.exists() or path.is_symlink() for path in (config_path, secret_path)),
           "Configuration already exists; no credentials rotated.")
    d.need(Path(compose_file).is_file(), "Production Compose file not found.")
    c = d.read_json(d.ROOT / "config/production.example.json")
    for role, (service, container) in MAPPING.items():
        c[role + "_container"] = container
        c[role + "_compose_service"] = service
    c.update(env="prod", agent_name="eminerba-agent", web_service="eminerba-web", api_service="eminerba-api",
             mysql_host="db", db_apps=["web", "api"], artifact_dir="/opt/eminerba-observability/artifacts",
             output_dir="/opt/eminerba-observability/generated", agent_run_dir="/opt/eminerba-observability/agent-run",
             mysql_config_target="/etc/mysql/conf.d/zz-datadog.cnf")
    app = d.Deployment(c)
    selected = {role: app.inspect(container) for role, (_, container) in MAPPING.items()}
    projects = {item["project"] for item in selected.values()}
    d.need(len(projects) == 1 and all(projects), "Production containers must belong to one Compose project.")
    common = set.intersection(*(set(item["networks"]) for item in selected.values()))
    d.need(len(common) == 1, "Multiple/no shared networks; use explicit reviewed production configuration.")
    c["network"] = common.pop()
    schemas = json.loads(app.docker("exec", c["web_container"], "php", "-r",
                                   "echo json_encode([getenv('DB_NAME'),getenv('DB_NAME2')]);"))
    schemas = list(dict.fromkeys(value for value in schemas if value))
    for schema in schemas:
        d.identifier(schema)
    if schemas:
        c["mysql_schemas"] = schemas
    app.fetch()
    d.secure_write(config_path, d.jdump(c))
    d.secure_write(secret_path, d.jdump({"api_key": "REPLACE_NEW_DATADOG_API_KEY",
                                       "db_password": secrets.token_hex(24), "admin_password": ""}))
    print("Prepared " + str(config_path) + " and " + str(secret_path) + ". Fill placeholders/admin password locally.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply"), nargs="?", default="apply")
    parser.add_argument("--compose-file", default="/opt/eminerba_docker/docker-compose.yml")
    parser.add_argument("--config", type=Path, default=d.ROOT / "config/eminerba.json")
    parser.add_argument("--secrets", type=Path, default=d.ROOT / "config/eminerba-secrets.json")
    parser.add_argument("--maintenance", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    coordinator = None
    try:
        d.need(sys.platform.startswith("linux") and os.geteuid() == 0, "Run on the production Ubuntu host using sudo.")
        if args.action == "apply":
            d.need(args.dry_run or args.maintenance, "Application/DB recreation requires --maintenance.")
        # Serialize runs across configurations; no competing installer processes.
        import fcntl
        with open("/run/lock/eminerba-observability.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise d.Failure("Another production run is active.") from exc
            if args.action == "prepare":
                d.need(not args.dry_run, "prepare writes configuration; use apply --dry-run after setup.")
                prepare(args.config, args.secrets, args.compose_file)
                return 0
            c = d.read_json(args.config)
            credentials = d.read_json(args.secrets, secret=True)
            coordinator = Production(d.Deployment(c, credentials), args.compose_file)
            coordinator.run(dry=args.dry_run)
        return 0
    except (d.Failure, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        stage = coordinator.stage if coordinator else "setup"
        message = str(exc) if isinstance(exc, d.Failure) else "Invalid local configuration/data; details withheld to protect secrets."
        print("ERROR production " + stage + ": " + message, file=sys.stderr)
        print("Stopped; no automatic rollback or volume deletion. Inspect the failed stage before rerunning.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
