#!/usr/bin/env python3
"""Staged Sucofindo production deployment; Python >=3.8, standard library only."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_output import Failure, Redactor, command_error, command_label, failure_context, log, register_secrets, report_error

ROOT = Path(__file__).resolve().parent.parent
SSI_URL = "https://install.datadoghq.com/scripts/install_script_agent7.sh"
RUM_URL = "https://rum-auto-instrumentation.s3.amazonaws.com/installer/latest/install-proxy-datadog.sh"
CAPS = ["SYS_ADMIN", "SYS_RESOURCE", "SYS_PTRACE", "NET_ADMIN",
        "NET_BROADCAST", "NET_RAW", "IPC_LOCK", "CHOWN"]
SQL_MODE = "SET SESSION sql_mode='NO_BACKSLASH_ESCAPES';\nSET NAMES utf8mb4;\n"
PHP_PROBE = """$files=array_filter(array_merge([php_ini_loaded_file()],
explode(',',php_ini_scanned_files() ?: '')));
$manual=[]; foreach($files as $f){$f=trim($f);
if(is_readable($f) && preg_match('/^\\s*(?:zend_)?extension\\s*=.*ddtrace/im',file_get_contents($f)))
{$manual[]=basename($f);}}
echo json_encode([
'version'=>PHP_VERSION,'sapi'=>PHP_SAPI,
'ddtrace'=>phpversion('ddtrace') ?: null,
'pdo'=>class_exists('PDO') ? PDO::getAvailableDrivers() : [],
'mysqli'=>extension_loaded('mysqli'),
'conflicts'=>array_values(array_intersect(array_map('strtolower',get_loaded_extensions()),
['xdebug','ioncube loader','newrelic','blackfire','pcov'])),
'jit'=>ini_get('opcache.jit'),'jit_buffer'=>ini_get('opcache.jit_buffer_size'),
'manual_ddtrace_ini'=>$manual]);"""
APACHE_PROBE = "command -v apache2ctl || command -v apachectl || command -v httpd"
LOCK_PATH = Path("/run/lock/eminerba-observability.lock")
RUM_LOCK = "/run/datadog-rum-installer.lock"
RUM_SUPERVISOR = '''setsid timeout --kill-after=5s "$@" &
job=$!
trap 'kill -KILL "-$job" 2>/dev/null || :' 0
trap 'exit 143' TERM
trap 'exit 130' INT
wait "$job"
status=$?
exit "$status"
'''


def need(condition, message):
    if not condition:
        raise Failure(message)


@contextmanager
def deployment_lock(path=LOCK_PATH):
    """One host lock for all Python deployment entry points; never unlink it."""
    need(os.name == "posix", "Run deployment commands on the target Ubuntu host.")
    import fcntl
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        need(stat.S_ISREG(os.fstat(lock.fileno()).st_mode), "Deployment lock must be a regular file.")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure("Another deployment/staged command is active; retry after it finishes.") from exc
        def interrupt(signum, frame):
            raise KeyboardInterrupt()
        previous = signal.signal(signal.SIGTERM, interrupt)
        try:
            yield
        finally:
            signal.signal(signal.SIGTERM, previous)


def jdump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def docker_endpoint(context_endpoint):
    # Docker CLI gives an explicit DOCKER_CONTEXT priority over DOCKER_HOST.
    return context_endpoint if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST") or context_endpoint


def identifier(value):
    need(isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value),
         "Invalid SQL identifier.")
    return chr(96) + value + chr(96)


def literal(value):
    # Always used under explicit NO_BACKSLASH_ESCAPES, never caller SQL mode.
    need(isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value),
         "SQL values must not contain control characters.")
    return "'" + value.replace("'", "''") + "'"


def filled(value):
    return isinstance(value, str) and bool(value) and not re.search(r"REPLACE|CHANGE_ME|[<>]", value, re.I)


def secure_write(path, content):
    path = Path(path)
    need(not path.is_symlink(), "Refusing to write through an output symlink.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".dd-", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, secret=False):
    path = Path(path)
    need(path.is_file() and not path.is_symlink(), "Configuration file is missing or is a symlink: " + str(path))
    if secret and os.name != "nt":
        need(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "Secret files must have mode 0600; run chmod 600 on: " + str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Failure("Invalid configuration JSON: " + str(path),
                      details="Line {}, column {}: {}".format(exc.lineno, exc.colno, exc.msg),
                      hint="Correct the JSON syntax at the reported location; do not paste secret file contents into logs.") from exc
    need(isinstance(value, dict), "Configuration must be a JSON object.")
    return value


def validate(c, full=False):
    required = ("web_container", "api_container", "mysql_container", "network")
    for key in required:
        need(filled(c.get(key)) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", c[key]),
             "Set an explicit configuration value: " + key + " (use discover to identify the target).")
    need(len({c[k] for k in required[:3]}) == 3, "Web, API and MySQL must use different containers.")
    need(isinstance(c.get("env"), str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", c["env"])
         and filled(c["env"]), "env must be a lowercase environment name, e.g. prod or lab.")
    need(type(c.get("timeout")) is int and 1 <= c["timeout"] <= 1800, "timeout must be an integer from 1 to 1800 seconds.")
    need(type(c.get("mysql_port")) is int and 1 <= c["mysql_port"] <= 65535, "mysql_port must be an integer from 1 to 65535.")
    for key in ("runtime_security", "network_monitoring", "universal_service_monitoring", "process_collection"):
        need(type(c.get(key)) is bool, key + " must be a boolean.")
    for key in ("output_dir", "artifact_dir", "socket_dir", "agent_run_dir", "docker_socket",
                "container_logs_dir", "mysql_data_dir", "mysql_config_target"):
        value = c.get(key, "")
        need(value.startswith("/") and value != "/" and not re.search(r"[\s:$\\]", value)
             and ".." not in value.split("/"), key + " must be an absolute Linux path without spaces, dollar signs or colons.")
    need(c["socket_dir"] == "/var/run/datadog", "SSI requires socket_dir=/var/run/datadog.")
    if not full:
        return
    for key in ("agent_name", "site", "version", "web_service", "api_service", "mysql_host",
                "db_user", "db_user_host", "rum_application_id", "rum_client_token",
                "rum_remote_configuration_id"):
        need(filled(c.get(key)) and re.fullmatch(r"[A-Za-z0-9_.%:/-]+", c[key]),
             "Invalid or missing configuration value: " + key)
    need(c["agent_name"] not in {c[k] for k in required[:3]}, "The Agent name conflicts with an application container.")
    need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", c["agent_name"]), "Invalid agent_name.")
    match = re.fullmatch(r"registry\.datadoghq\.com/agent:7\.(\d+)\.(\d+)", c.get("agent_image", ""))
    need(match is not None and tuple(map(int, match.groups())) >= (76, 1),
         "Pin agent_image registry.datadoghq.com/agent:7.X.Y >=7.76.1.")
    schemas = c.get("mysql_schemas")
    need(isinstance(schemas, list) and schemas and len(set(schemas)) == len(schemas),
         "mysql_schemas must be a nonempty list of unique schema names.")
    for schema in schemas:
        need(filled(schema), "Replace the schema placeholder with an actual application database.")
        identifier(schema)
        need(schema not in ("mysql", "sys", "performance_schema", "information_schema", "datadog"),
             "Select an application schema, not a system schema.")
    identifier(c["db_user"])
    need(set(c.get("db_apps", [])) <= {"web", "api"} and c.get("db_apps"), "db_apps must contain web and/or api.")
    for key in ("rum_url", "apm_url"):
        value = c.get(key)
        if value is None or value == "":
            continue
        need(filled(value) and re.fullmatch(r"https?://[^@\s]+", value),
             key + " must be an HTTP(S) URL without embedded credentials, or empty to skip HTTP checks.")


class Runner:
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.redactor = Redactor()

    def run(self, args, data=None, env=None, check=True, timeout=None):
        env = dict(os.environ if env is None else env)
        env["LC_ALL"] = "C.UTF-8"
        sensitive = [value for key, value in env.items()
                     if key.upper() not in ("PWD", "OLDPWD") and
                     re.search(r"password|passwd|pwd|secret|token|api.?key|credential|db_pass", key, re.I)]
        self.redactor.add(sensitive)
        register_secrets(sensitive)
        try:
            if os.name == "posix":
                process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env,
                    shell=False, start_new_session=True)
                try:
                    stdout, stderr = process.communicate(input=data, timeout=timeout or self.timeout)
                except BaseException:
                    self.stop_group(process)
                    raise
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream:
                            stream.close()
                result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
            else:
                # Windows is for offline tooling/tests, not host deployment.
                result = subprocess.run(args, input=data, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=timeout or self.timeout,
                                        env=env, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise Failure("Command timed out after {} seconds.".format(timeout or self.timeout),
                          command=command_label(args),
                          details=self.redactor.clean(exc.stderr)[-4000:] or "No stderr diagnostic was captured before timeout.",
                          hint="Check service health and network access. Inspect partial changes before retrying an installer.") from exc
        except OSError as exc:
            raise Failure("Could not start the command.", command=command_label(args),
                          details=self.redactor.clean(exc.strerror or type(exc).__name__),
                          hint="Check that the executable is installed and accessible to the deployment user.") from exc
        if check and result.returncode:
            raise command_error(args, result, self.redactor)
        return result

    @staticmethod
    def stop_group(process):
        """Terminate the local session's process group, then escalate within 5 s."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # Children may still exist even after the immediate parent exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


class Deployment:
    def __init__(self, config, secrets=None, runner=None, dry=False):
        self.c = config
        self.s = secrets or {}
        self.r = runner or Runner(config.get("timeout", 60))
        sensitive = list(self.s.values()) + [config.get("rum_client_token", "")]
        register_secrets(sensitive)
        if isinstance(self.r, Runner):
            self.r.redactor.add(sensitive)
        self.dry = dry
        self.out = Path(config.get("output_dir", "generated"))

    def run(self, args, include_stderr=False, **kw):
        result = self.r.run(args, **kw)
        output = result.stdout
        if include_stderr:
            output += "\n" + result.stderr
        return output.strip()

    def docker(self, *args, **kw):
        return self.run(["docker", *args], **kw)

    def inspect(self, name):
        # Explicit whitelist: never request Config.Env or full inspect.
        fields = {
            "id": ".Id", "name": ".Name", "image": ".Config.Image",
            "running": ".State.Running", "runtime": ".HostConfig.Runtime",
            "mounts": ".Mounts", "networks": ".NetworkSettings.Networks",
            "service": 'index .Config.Labels "com.docker.compose.service"',
            "project": 'index .Config.Labels "com.docker.compose.project"',
            "managed": 'index .Config.Labels "id.sucofindo.datadog.managed"',
            "spec": 'index .Config.Labels "id.sucofindo.datadog.spec"',
        }
        # Parentheses make index lookups a single argument to Go's json function.
        return {k: json.loads(self.docker("inspect", "--format", "{{json (" + expr + ")}}", name))
                for k, expr in fields.items()}

    def discover(self):
        info = json.loads(self.docker("info", "--format", "{{json .}}"))
        containers = [self.inspect(n) for n in self.docker("ps", "-a", "--format", "{{.Names}}").splitlines()]
        reduced = []
        for item in containers:
            row = {k: item[k] for k in ("name", "image", "running", "service", "project", "runtime")}
            row["networks"] = sorted(item["networks"])
            reduced.append(row)
        packages = self.run(["dpkg-query", "-W", "-f=$" + "{binary:Package} $" + "{Version}\\n",
                             "datadog-apm-inject", "datadog-apm-library-php"], check=False).splitlines()
        for name in ("datadog-apm-inject", "datadog-apm-library-php"):
            package = Path("/opt/datadog-packages") / name
            if package.exists():
                packages.append(name + " " + str(package.resolve()))
        return {
            "os": platform.system(), "arch": platform.machine(),
            "docker_version": info.get("ServerVersion"), "docker_os": info.get("OperatingSystem"),
            "kernel": info.get("KernelVersion"), "default_runtime": info.get("DefaultRuntime"),
            "rootless": any("rootless" in x for x in info.get("SecurityOptions", [])),
            "docker_root": info.get("DockerRootDir"), "containers": reduced,
            "networks": self.docker("network", "ls", "--format", "{{.Name}}").splitlines(),
            "agent_candidates": [x["name"].lstrip("/") for x in containers
                                 if re.search(r"(datadog|dd-agent)", x["image"] + x["name"], re.I)],
            "host_agent_active": self.r.run(["systemctl", "is-active", "datadog-agent"],
                                            check=False).returncode == 0,
            "ssi_packages": packages,
        }

    def preflight(self, full=False, require_rum_tools=True):
        validate(self.c, full)
        c = self.c
        need(platform.system() == "Linux", "Run preflight on the target Ubuntu host.")
        release = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                release[key] = val.strip('"')
        need(release.get("ID") == "ubuntu" and release.get("VERSION_ID", "").split(".")[0]
             in ("20", "22", "24"), "This Ubuntu version is outside the supported SSI 20/22/24 LTS matrix; review compatibility.")
        need(platform.machine() in ("x86_64", "aarch64", "arm64"), "Unsupported architecture for SSI.")
        for tool in ("docker", "bash", "curl", "tar", "sha256sum", "systemctl", "dpkg-query"):
            need(shutil.which(tool), "Missing host prerequisite: " + tool)
        inventory = self.discover()
        need(not inventory["rootless"], "Host SSI and telemetry require rootful Docker.")
        # Docker context/DOCKER_HOST may target another machine. Host mounts and SSI
        # are safe only when talking to the daemon on this host.
        endpoint = self.docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
        effective_endpoint = docker_endpoint(endpoint)
        need(effective_endpoint == "unix://" + c["docker_socket"],
             "Docker does not target the configured local socket; do not install host SSI against a remote daemon.")
        selected = {role: self.inspect(c[role + "_container"]) for role in ("web", "api", "mysql")}
        for role, item in selected.items():
            need(item["running"], "Container " + role + " is not running.")
            need(c["network"] in item["networks"], "The configured network is not attached to " + role)
        db_net = selected["mysql"]["networks"][c["network"]]
        db_aliases = set(db_net.get("Aliases") or []) | {
            c["mysql_container"], db_net.get("IPAddress"), db_net.get("GlobalIPv6Address")}
        need(c["mysql_host"] in db_aliases,
             "mysql_host is not an alias/IP of the selected database on the target network.")
        persistent = [m for m in selected["mysql"]["mounts"] if m["Type"] in ("volume", "bind") and m.get("RW")
                      and (c["mysql_data_dir"] == m["Destination"] or
                           c["mysql_data_dir"].startswith(m["Destination"].rstrip("/") + "/"))]
        need(persistent, "Persistent MySQL storage could not be verified; ask the DBA to check the datadir mount.")
        version = mysql_version(self.docker("exec", c["mysql_container"], "mysqld", "--version"))
        php, warnings = {}, []
        for role in ("web", "api"):
            php[role] = json.loads(self.docker("exec", c[role + "_container"], "php", "-r", PHP_PROBE))
            p = php[role]
            need(p["version"].startswith("8.1."), "The running PHP version differs from the required 8.1 target: " + role)
            need(not p["conflicts"], "Conflicting PHP extensions prevent SSI on " + role)
            need(not p.get("manual_ddtrace_ini"), "Manual tracer configuration was detected on " + role
                 + "; remove the conflict before enabling SSI.")
            need(p["jit"] in ("", "0", "off", "disable", None) or
                 p["jit_buffer"] in ("", "0", None), "PHP JIT is enabled; review compatibility before enabling SSI.")
            if p["ddtrace"] and inventory["default_runtime"] != "dd-shim":
                raise Failure("An existing tracer was found without verified SSI; review potential duplicate instrumentation.")
            if role in c.get("db_apps", []) and "mysql" not in p["pdo"]:
                warnings.append(role + ": PDO MySQL is unavailable; APM-DBM correlation has not been verified.")
            if p["mysqli"]:
                warnings.append(role + ": MySQLi is installed; confirm the actual CodeIgniter database driver.")
        apache = self.docker("exec", c["web_container"], "sh", "-c", APACHE_PROBE).splitlines()[0]
        need(re.fullmatch(r"/[A-Za-z0-9_./-]+", apache), "Invalid Apache executable path.")
        apache_v = self.docker("exec", c["web_container"], apache, "-V")
        root = re.search(r'HTTPD_ROOT="([^"]+)"', apache_v)
        need(root is not None, "Could not detect Apache HTTPD_ROOT.")
        apache_root = root.group(1)
        need(apache_root in ("/etc/apache2", "/usr/local/apache2", "/etc/httpd"),
             "Custom HTTPD_ROOT requires a RUM persistence review.")
        conf = re.search(r'SERVER_CONFIG_FILE="([^"]+)"', apache_v)
        need(conf is not None, "Could not detect Apache SERVER_CONFIG_FILE.")
        apache_config = conf.group(1)
        if not apache_config.startswith("/"):
            apache_config = apache_root + "/" + apache_config
        need(apache_config.startswith(apache_root + "/") and ".." not in apache_config.split("/"),
             "Apache configuration is outside the backup root; review the layout.")
        self.docker("exec", c["web_container"], apache, "-t")
        if require_rum_tools:
            for tool in ("curl", "tar", "gzip", "gpg", "sh", "timeout", "flock", "setsid"):
                self.docker("exec", c["web_container"], "sh", "-c", 'command -v "$1"', "sh", tool)
        for role in ("web", "api"):
            self.docker("exec", c[role + "_container"], "php", "-r",
                        '$s=@fsockopen($argv[1],(int)$argv[2],$e,$m,5); exit($s?0:1);',
                        c["mysql_host"], str(c["mysql_port"]))
        warnings.append("PHP CLI checks do not verify the web SAPI; validate HTTP tracing and the CodeIgniter driver after recreation.")
        if c["network_monitoring"] or c["universal_service_monitoring"] or c["runtime_security"]:
            kernel = version_tuple(inventory["kernel"])
            need(kernel >= (4, 14, 0), "system-probe features require a compatible kernel >=4.14.")
            need(Path("/sys/kernel/debug").exists(), "Host debugfs is unavailable.")
        return {"inventory": inventory, "ubuntu": release["VERSION_ID"], "mysql_version": version,
                "mysql_persistence": [{"type": m["Type"], "target": m["Destination"]} for m in persistent],
                "selected": selected, "php": php, "apache": apache, "apache_root": apache_root,
                "apache_config": apache_config,
                "warnings": warnings, "telemetry": "PENDING_BROWSER_AND_APPLICATION_TRAFFIC"}

    def secret(self, key):
        value = self.s.get(key)
        need(isinstance(value, str) and value and "REPLACE" not in value
             and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value), "Invalid or missing secret: " + key)
        return value

    def service(self, report, role):
        label = report["selected"][role]["service"]
        explicit = self.c.get(role + "_compose_service")
        need(not (label and explicit and label != explicit),
             "The explicit service mapping does not match the Compose label: " + role)
        value = label or explicit
        need(value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value),
             "Missing Compose service label; set " + role + "_compose_service.")
        return value

    def render(self, report):
        c = self.c
        need(len({self.service(report, role) for role in ("web", "api", "mysql")}) == 3,
             "Duplicate Compose service mapping; review the project and service configuration.")
        projects = {item.get("project") for item in report["selected"].values() if item.get("project")}
        need(len(projects) <= 1, "Containers belong to different Compose projects; create separate reviewed overrides.")
        self.secret("api_key")
        self.secret("db_password")
        services = {}
        for role in ("web", "api"):
            env = json.loads((ROOT / "datadog/templates/application.json").read_text(encoding="utf-8"))
            env.update(DD_ENV=c["env"], DD_SERVICE=c[role + "_service"], DD_VERSION=c["version"],
                       DD_DBM_PROPAGATION_MODE="full" if role in c["db_apps"] else "disabled")
            services[self.service(report, role)] = {
                "environment": env,
                "labels": {"com.datadoghq.tags.env": c["env"],
                           "com.datadoghq.tags.service": c[role + "_service"],
                           "com.datadoghq.tags.version": c["version"]},
                "volumes": [c["socket_dir"] + ":/var/run/datadog:ro"]}
        services[self.service(report, "mysql")] = {
            "volumes": [str(self.out / "99-datadog.cnf") + ":" + c["mysql_config_target"] + ":ro"]}
        instance = {"dbm": True, "host": c["mysql_host"], "port": c["mysql_port"],
                    "username": c["db_user"], "password": self.secret("db_password"),
                    "tags": ["env:" + c["env"], "service:" + c["mysql_container"]]}
        artifacts = {
            "application.override.json": jdump({"services": services}),
            # JSON is a YAML subset: no third-party YAML parser or secret interpolation.
            "mysql.d/conf.yaml": jdump({"init_config": {}, "instances": [instance]}),
            "99-datadog.cnf": (ROOT / "datadog/mysql/99-datadog.cnf").read_text(),
            "discovery.json": jdump(report),
            "agent-spec.json": jdump(self.agent_spec()),
        }
        if self.dry:
            log("DRY-RUN: render " + ", ".join(artifacts) + "; no files written.")
            return
        for name in ("mysql.d/conf.yaml", "agent-spec.json"):
            path = self.out / name
            need(not path.exists() or path.read_text(encoding="utf-8") == artifacts[name],
                 "Agent configuration changed; use a new output_dir and review Agent migration.")
        for name, body in artifacts.items():
            path = self.out / name
            need(not path.is_symlink(), "Refusing to write through an output symlink.")
            # Preserve inode for bind-mounted files on unchanged reruns.
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                secure_write(path, body)
            if name == "99-datadog.cnf":
                # No secrets: mysqld runs as a non-root user and must read this
                # bind mount. Repair older renders without replacing their inode.
                path.chmod(0o644)
        if getattr(self, "coordinated", False):
            log("Application, Agent and MySQL configuration generated.", "OK")
        else:
            log("HANDOFF: Review application.override.json, apply its mounts/configuration, then recreate the database/application containers.")

    def agent_spec(self):
        c = self.c
        env = json.loads((ROOT / "datadog/templates/agent.json").read_text())
        env.update(DD_SITE=c["site"], DD_ENV=c["env"],
                   DD_CONTAINER_EXCLUDE_LOGS="name:" + c["agent_name"],
                   DD_PROCESS_AGENT_ENABLED=str(c["process_collection"]).lower(),
                   DD_PROCESS_CONFIG_PROCESS_COLLECTION_ENABLED=str(c["process_collection"]).lower(),
                   DD_RUNTIME_SECURITY_CONFIG_ENABLED=str(c["runtime_security"]).lower(),
                   DD_SYSTEM_PROBE_NETWORK_ENABLED=str(c["network_monitoring"]).lower(),
                   DD_SYSTEM_PROBE_SERVICE_MONITORING_ENABLED=str(c["universal_service_monitoring"]).lower())
        mounts = [
            c["socket_dir"] + ":/var/run/datadog",
            c["agent_run_dir"] + ":/opt/datadog-agent/run:rw",
            c["docker_socket"] + ":/var/run/docker.sock:ro",
            "/proc:/host/proc:ro", "/sys/fs/cgroup:/host/sys/fs/cgroup:ro",
            c["container_logs_dir"] + ":/var/lib/docker/containers:ro",
            str(self.out / "mysql.d/conf.yaml") + ":/etc/datadog-agent/conf.d/mysql.d/conf.yaml:ro"]
        features = any(c[x] for x in ("runtime_security", "network_monitoring", "universal_service_monitoring"))
        if features:
            mounts += ["/sys/kernel/debug:/sys/kernel/debug"]
        if c["runtime_security"] or c["universal_service_monitoring"]:
            env["HOST_ROOT"] = "/host/root"
            mounts += ["/:/host/root:ro", "/etc/os-release:/etc/os-release:ro"]
        if c["runtime_security"] or c["process_collection"]:
            mounts += ["/etc/passwd:/etc/passwd:ro", "/etc/group:/etc/group:ro"]
        return {"image": c["agent_image"], "name": c["agent_name"], "network": c["network"],
                "environment": env, "mounts": mounts, "caps": CAPS if features else [],
                "host_pid": features or c["process_collection"], "host_cgroup": features,
                "unconfined_apparmor": features, "restart": "unless-stopped"}

    def start_agent(self, report):
        spec = self.agent_spec()
        config_path = self.out / "mysql.d/conf.yaml"
        need(config_path.is_file(), "Run the render stage first.")
        spec_hash = hashlib.sha256((jdump(spec) + config_path.read_text(encoding="utf-8")
                                   + self.secret("api_key")).encode()).hexdigest()
        inv = report["inventory"]
        candidates = inv["agent_candidates"]
        need(not inv["host_agent_active"], "A host Agent is active; resolve ownership before continuing with one Agent per host.")
        if candidates:
            need(candidates == [self.c["agent_name"]], "Another Agent already exists; resolve ownership before starting this Agent.")
            existing = self.inspect(candidates[0])
            need(existing["managed"] == "true" and existing["spec"] == spec_hash,
                 "The existing Agent is unmanaged or its specification changed; review it before replacement.")
            need(existing["running"], "The managed Agent is stopped; inspect it and start it explicitly.")
            self.wait_agent()
            log("Managed Agent is running with the expected specification; creation skipped.")
            return
        self.secret("api_key")
        for mount in spec["mounts"]:
            source = mount.split(":")[0]
            if source not in (self.c["socket_dir"], self.c["agent_run_dir"]):
                need(Path(source).exists(), "Host mount source does not exist: " + source)
        if self.dry:
            log("DRY-RUN: create a dedicated Agent; environment secrets are redacted.")
            return
        for key in ("socket_dir", "agent_run_dir"):
            Path(self.c[key]).mkdir(parents=True, exist_ok=True, mode=0o755)
        args = ["run", "-d", "--name", spec["name"], "--restart", spec["restart"],
                "--network", spec["network"], "--label", "id.sucofindo.datadog.managed=true",
                "--label", "id.sucofindo.datadog.spec=" + spec_hash,
                "--health-cmd", "agent health", "--health-interval", "15s",
                "--health-timeout", "5s", "--health-retries", "8", "--env", "DD_API_KEY"]
        for key, val in spec["environment"].items():
            args += ["--env", key + "=" + val]
        for mount in spec["mounts"]:
            args += ["--volume", mount]
        for cap in spec["caps"]:
            args += ["--cap-add", cap]
        if spec["host_pid"]:
            args += ["--pid", "host"]
        if spec["host_cgroup"]:
            args += ["--cgroupns", "host"]
        if spec["unconfined_apparmor"]:
            args += ["--security-opt", "apparmor:unconfined"]
        env = os.environ.copy()
        env["DD_API_KEY"] = self.secret("api_key")
        self.docker(*args, spec["image"], env=env, timeout=600)
        self.wait_agent()
        log("Agent local health passed; verify telemetry delivery in Datadog.")

    def wait_agent(self):
        deadline = time.monotonic() + self.c["timeout"]
        last_error = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure_context("Agent health check timed out.", last_error)
            args = ["docker", "exec", self.c["agent_name"], "agent", "health"]
            try:
                result = self.r.run(args, check=False, timeout=min(10, remaining))
                if result.returncode == 0:
                    return
                last_error = command_error(args, result)
            except Failure as exc:
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure_context("Agent health check timed out.", last_error)
            time.sleep(min(2, remaining))

    def wait_probe(self, args, label):
        deadline = time.monotonic() + max(300, self.c["timeout"])
        last_error = None
        message = "Readiness timed out: " + label + "; inspect service logs locally."
        log("Waiting for " + label + " (up to " + str(max(300, self.c["timeout"])) + " seconds).")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure_context(message, last_error)
            try:
                result = self.r.run(args, check=False, timeout=min(10, remaining))
                if result.returncode == 0:
                    log(label + " is ready.", "OK")
                    return
                last_error = command_error(args, result)
            except Failure as exc:
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure_context(message, last_error)
            time.sleep(min(2, remaining))

    def wait_application(self, role):
        need(role in ("web", "api"), "Unsupported application readiness role.")
        self.wait_probe(["docker", "exec", self.c[role + "_container"], "php", "-r",
                         'exit(@fsockopen("127.0.0.1",80,$e,$m,2) ? 0 : 1);'], role + " Apache listener")

    def wait_http(self):
        for key in ("apm_url", "rum_url"):
            url = self.c.get(key)
            if not url:
                log("Skipping " + key + " HTTP readiness: no URL configured.")
                continue
            self.wait_probe(["curl", "--fail", "--silent", "--show-error", "--max-time", "8",
                             "--output", "/dev/null", url], key + " HTTP response")

    def fetch(self):
        if self.dry:
            log("DRY-RUN: download two installers into artifact_dir for review; no downloads performed.")
            return
        for name, url in (("ssi.sh", SSI_URL), ("rum.sh", RUM_URL)):
            path = Path(self.c["artifact_dir"]) / name
            if path.exists():
                log(name + " already exists; SHA256=" + hashlib.sha256(path.read_bytes()).hexdigest())
                continue
            try:
                with urllib.request.urlopen(url, timeout=self.c["timeout"]) as response:
                    body = response.read(4 * 1024 * 1024 + 1)
                need(len(body) <= 4 * 1024 * 1024 and body.startswith(b"#!"), "The downloaded artifact is not a supported installer script.")
                secure_write(path, body.decode("utf-8"))
            except (OSError, UnicodeError) as exc:
                raise Failure("Installer download failed; no installer was executed.",
                              details=Redactor(self.s.values()).clean(str(exc))[:4000],
                              hint="Check outbound HTTPS, DNS and the installer download endpoint before retrying.") from exc
            log(name + " SHA256=" + hashlib.sha256(path.read_bytes()).hexdigest())
        log("Review the scripts and downstream downloads, then record the approved hashes in the active configuration.")

    def artifact(self, name):
        path = Path(self.c["artifact_dir"]) / (name + ".sh")
        expected = self.c.get(name + "_sha256", "")
        need(re.fullmatch(r"[a-fA-F0-9]{64}", expected) and len(set(expected)) > 1,
             "Set the reviewed checksum: " + name + "_sha256.")
        need(path.is_file() and not path.is_symlink(), "Installer artifact is missing: " + name)
        need(hashlib.sha256(path.read_bytes()).hexdigest() == expected.lower(), "Installer checksum mismatch; review the artifact before updating the approved hash.")
        return path

    def install_ssi(self, report):
        inventory = report["inventory"]
        if inventory["default_runtime"] == "dd-shim":
            need(any("datadog-apm-library-php" in x for x in inventory["ssi_packages"]),
                 "dd-shim exists but the PHP SSI package could not be verified; review the installation.")
            log("Existing Docker SSI detected; installation skipped. Verify injection after container recreation.")
            return
        need(not inventory["ssi_packages"], "Partial SSI installation detected; review it before retrying.")
        path = self.artifact("ssi")
        if self.dry:
            log("DRY-RUN: install host Docker SSI with php:1 and DD_NO_AGENT_INSTALL=true; container recreation is required.")
            return
        need(os.geteuid() == 0, "Run ssi-install as root during an approved maintenance window.")
        backup = self.out / ("ssi-before-" + str(time.time_ns()))
        for source in (Path("/etc/docker/daemon.json"), Path("/etc/ld.so.preload")):
            if source.exists():
                secure_write(backup / source.name, source.read_text())
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("DD_"):
                del env[key]
        env.update(DD_APM_INSTRUMENTATION_LIBRARIES="php:1",
                   DD_APM_INSTRUMENTATION_ENABLED="docker", DD_NO_AGENT_INSTALL="true")
        self.run(["bash", str(path)], env=env, timeout=1800)
        need(self.docker("info", "--format", "{{.DefaultRuntime}}") == "dd-shim",
             "The installer exited but Docker's default runtime is not dd-shim.")
        if getattr(self, "coordinated", False):
            log("Host SSI installed. Application recreation will follow.", "OK")
        else:
            log("HANDOFF SSI: Recreate application containers to apply SSI; a restart alone does not inject the tracer.")

    def mysql(self, sql, admin=True, timeout=None):
        """Password via stdin, never Docker argv or container environment config."""
        password = self.secret("admin_password" if admin else "db_password")
        user = self.c["admin_user" if admin else "db_user"]
        # Loopback RSA exchange supports caching_sha2_password; loose keeps older 5.7 clients compatible.
        script = ('IFS= read -r MYSQL_PWD; export MYSQL_PWD; '
                  'exec mysql --loose-get-server-public-key --protocol=tcp --connect-timeout=5 '
                  '--host=127.0.0.1 --port="$1" --user="$2" '
                  '--batch --raw --skip-column-names --binary-mode --default-character-set=utf8mb4 '
                  '--init-command="SET SESSION sql_mode=\'NO_BACKSLASH_ESCAPES\'"')
        return self.docker("exec", "-i", self.c["mysql_container"], "sh", "-c", script,
                           "sh", str(self.c["mysql_port"]), user, data=password + "\n" + SQL_MODE + sql,
                           **({"timeout": timeout} if timeout is not None else {}))

    def procedures(self):
        result = {}
        explain = (ROOT / "datadog/templates/explain.sql").read_text().strip()
        for schema in ["datadog"] + self.c["mysql_schemas"]:
            result[(schema, "explain_statement")] = explain.format(schema=identifier(schema))
        result[("datadog", "enable_events_statements_consumers")] = (
            ROOT / "datadog/templates/consumers.sql").read_text().strip()
        return result

    def check_mysql_schemas(self):
        for schema in self.c["mysql_schemas"]:
            need(self.mysql("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name="
                            + literal(schema) + ";") == "1", "Application schema not found: " + schema)

    def dbm_sql(self, report, export=False):
        c = self.c
        account = literal(c["db_user"]) + "@" + literal(c["db_user_host"])
        create = ("CREATE USER IF NOT EXISTS " + account + " IDENTIFIED BY "
                  + literal(self.secret("db_password")) + " WITH MAX_USER_CONNECTIONS 5;\n")
        # No ALTER USER: existing passwords/limits are never rotated implicitly.
        grants = ("GRANT REPLICATION CLIENT, PROCESS ON *.* TO " + account + ";\n"
                  "GRANT SELECT ON performance_schema.* TO " + account + ";\n"
                  "GRANT SELECT ON mysql.innodb_index_stats TO " + account + ";\n"
                  "CREATE SCHEMA IF NOT EXISTS datadog;\n")
        routines = self.procedures()
        if export:
            chunks = [SQL_MODE, "-- DBA: REVIEW first; run without --force. No DROP/ALTER USER.\n",
                      "-- Existing routines: compare SHOW CREATE PROCEDURE; omit identical CREATE only.\n",
                      "-- Existing account: verify password, grants and max_user_connections.\n",
                      "SELECT VERSION(), @@datadir, @@performance_schema;\n", create, grants]
            for (schema, name), ddl in routines.items():
                chunks += ["DELIMITER $$\n", ddl + "$$\nDELIMITER ;\n",
                           "GRANT EXECUTE ON PROCEDURE " + identifier(schema) + "." + name
                           + " TO " + account + ";\n"]
            chunks += ["CALL datadog.enable_events_statements_consumers();\n"]
            if self.dry:
                log("DRY-RUN: export credential-bearing DBA SQL; no file written or SQL displayed.")
            else:
                secure_write(self.out / "dbm-dba-review.sql", "".join(chunks))
                log("HANDOFF DBA: Review dbm-dba-review.sql (mode 0600) and existing database objects before execution.")
            return
        actual = self.mysql("SELECT VERSION();")
        need(mysql_version(actual)[:2] == report["mysql_version"][:2], "The MySQL server version changed.")
        datadir = self.mysql("SELECT @@datadir;").rstrip("/")
        need(datadir == c["mysql_data_dir"].rstrip("/"), "The server datadir differs from the configured mapping.")
        self.check_mysql_schemas()
        exists = self.mysql("SELECT COUNT(*) FROM mysql.user WHERE user=" + literal(c["db_user"])
                            + " AND host=" + literal(c["db_user_host"]) + ";") == "1"
        if exists:
            need(self.mysql("SELECT 1;", admin=False) == "1", "Existing DBM credentials do not match the configured credentials.")
            need(self.mysql("SELECT CURRENT_USER();", admin=False) == c["db_user"] + "@" + c["db_user_host"],
                 "The DBM connection matched a different account host; ask the DBA to review account selection.")
            limit = self.mysql("SELECT max_user_connections FROM mysql.user WHERE user="
                               + literal(c["db_user"]) + " AND host=" + literal(c["db_user_host"]) + ";")
            need(limit == "5", "The existing account has a different connection limit; ask the DBA to review it.")
        pending = []
        for (schema, name), ddl in routines.items():
            definition = self.mysql(
                "SELECT CONCAT(security_type,'|',COALESCE(routine_definition,'')) "
                "FROM information_schema.routines WHERE routine_schema=" + literal(schema)
                + " AND routine_name=" + literal(name) + " AND routine_type='PROCEDURE';")
            if definition:
                expected = ddl[ddl.index("BEGIN"):]
                need(definition.startswith("DEFINER|") and normalize_sql(definition.split("|", 1)[1])
                     == normalize_sql(expected),
                     "Existing procedure differs or could not be read: " + schema + "." + name)
                params = self.mysql(
                    "SELECT CONCAT(parameter_mode,':',data_type) FROM information_schema.parameters "
                    "WHERE specific_schema=" + literal(schema) + " AND specific_name=" + literal(name)
                    + " ORDER BY ordinal_position;")
                need(params == ("IN:text" if name == "explain_statement" else ""),
                     "Existing procedure signature differs: " + schema + "." + name)
            else:
                pending.append(ddl)
        if self.dry:
            log("DRY-RUN: DBM checks OK; user existing=" + str(exists)
                  + "; new procedures=" + str(len(pending)) + "; no SQL changes made.")
            return
        self.mysql(("" if exists else create) + grants)
        for ddl in pending:
            self.mysql("DELIMITER $$\n" + ddl + "$$\nDELIMITER ;\n")
        for schema, name in routines:
            self.mysql("GRANT EXECUTE ON PROCEDURE " + identifier(schema) + "." + name
                       + " TO " + account + ";")
        self.mysql("CALL datadog.enable_events_statements_consumers();")
        log("DBM SQL provisioning completed. Verify MySQL after applying the startup configuration.")

    def rum_connection(self):
        uri = "http://" + self.c["agent_name"] + ":8126"
        self.docker("exec", self.c["web_container"], "curl", "--fail", "--silent",
                    "--show-error", "--max-time", "10", uri + "/info")
        return uri

    def install_rum(self, report):
        c = self.c
        path = self.artifact("rum")
        uri = self.rum_connection()
        if not self.dry:
            self.rum_idle()
        modules = self.docker("exec", c["web_container"], report["apache"], "-M")
        if "datadog_module" in modules:
            log("Existing RUM module detected; installer skipped. Review configuration and run verify.")
            return
        if self.dry:
            log("DRY-RUN: back up Apache, install RUM, test configuration, reload gracefully and export persistent assets.")
            return
        backup = self.out / ("rum-before-" + str(time.time_ns()))
        backup.mkdir(parents=True, mode=0o700)
        self.docker("cp", c["web_container"] + ":" + report["apache_root"], str(backup / "apache"))
        secure_write(backup / "manifest.json", jdump({
            "container_id": report["selected"]["web"]["id"],
            "apache_root": report["apache_root"], "apache_config": report["apache_config"]}))
        work = self.docker("exec", "-u", "0", c["web_container"], "mktemp", "-d", "/tmp/dd-rum.XXXXXXXX")
        need(re.fullmatch(r"/tmp/dd-rum\.[A-Za-z0-9]+", work), "Invalid temporary RUM directory.")
        self.docker("cp", str(path), c["web_container"] + ":" + work + "/installer.sh")
        help_text = self.docker("exec", "-u", "0", "-w", work, c["web_container"],
                                "flock", "--nonblock", RUM_LOCK, "sh", "-c", RUM_SUPERVISOR, "sh", "600s",
                                "sh", "./installer.sh", "--help", timeout=615,
                                include_stderr=True)
        for flag in ("proxyKind", "appId", "site", "clientToken", "remoteConfigurationId", "agentUri"):
            need(flag in help_text, "The RUM configurator does not advertise the required flag " + flag
                 + "; review installer compatibility before changing Apache.")
        self.docker("exec", "-u", "0", "-w", work, c["web_container"],
                    "flock", "--nonblock", RUM_LOCK, "sh", "-c", RUM_SUPERVISOR, "sh", "600s", "sh", "./installer.sh",
                    "--proxyKind", "httpd", "--appId", c["rum_application_id"],
                    "--site", c["site"], "--clientToken", c["rum_client_token"],
                    "--remoteConfigurationId", c["rum_remote_configuration_id"],
                    "--agentUri", uri, timeout=615)
        # The Apache module's own APM tracing is separate from SSI PHP. RUM only here.
        self.docker("exec", "-i", "-u", "0", c["web_container"], "sh", "-c",
                    'cat >> "$1"', "sh", report["apache_config"],
                    data="\n<IfModule datadog_module>\n  DatadogTracing Off\n</IfModule>\n")
        try:
            self.docker("exec", c["web_container"], report["apache"], "-t")
        except Failure as exc:
            raise Failure("Apache configuration test failed; reload was skipped. Restore Apache from " + str(backup)) from exc
        self.docker("exec", c["web_container"], report["apache"], "-k", "graceful")
        return self.export_rum(report)

    def rum_idle(self):
        # Also catches a bounded installer whose Docker client was disconnected.
        self.docker("exec", "-u", "0", self.c["web_container"], "flock", "--nonblock", RUM_LOCK, "true")

    def export_rum(self, report):
        if self.dry:
            log("DRY-RUN: export Apache, /opt/datadog-httpd and a persistence manifest.")
            return
        self.rum_idle()
        target = self.out / ("rum-persistence-" + str(time.time_ns()))
        target.mkdir(parents=True, mode=0o700)
        for source, name in ((report["apache_root"], "apache"), ("/opt/datadog-httpd", "module")):
            self.docker("cp", self.c["web_container"] + ":" + source, str(target / name))
        secure_write(target / "manifest.json", jdump({
            "container_id": report["selected"]["web"]["id"],
            "base_image": report["selected"]["web"]["image"],
            "apache_root": report["apache_root"],
            "module_root": "/opt/datadog-httpd",
            "warning": "Review LoadModule/Include paths; preserve referenced module files. No PHP tracer."
        }))
        secure_write(target / "Dockerfile.rum.example",
                     "ARG BASE_IMAGE\nFROM $" + "{BASE_IMAGE}\nUSER root\n"
                     "COPY module/ /opt/datadog-httpd/\n"
                     "COPY apache/ " + report["apache_root"] + "/\n"
                     "RUN " + report["apache"] + " -t\n"
                     "# Restore the original USER; keep the base image, distribution and Apache ABI compatible.\n")
        secure_write(target / "persistence.override.example.json", jdump({"services": {
            self.service(report, "web"): {"volumes": [
                str(target / "module") + ":/opt/datadog-httpd:ro",
                str(target / "apache") + ":" + report["apache_root"] + ":ro"]}}}))
        log("HANDOFF RUM persistence: " + str(target)
              + ". Review the exported configuration/assets, persist them and test after recreation.")
        return target

    def verify_mysql_startup(self, admin=False):
        names = ("performance_schema", "max_digest_length", "performance_schema_max_digest_length",
                 "performance_schema_max_sql_text_length")
        result = self.mysql("SELECT " + ",".join("@@" + name for name in names) + ";", admin=admin)
        values = result.split("\t")
        need(len(values) == 4 and all(re.fullmatch(r"[0-9]+", value) for value in values),
             "Could not read MySQL startup settings.")
        expected = ["1", "4096", "4096", "4096"]
        mismatch = [name + "=" + actual + " (expected " + wanted + ")"
                    for name, actual, wanted in zip(names, values, expected) if actual != wanted]
        need(not mismatch, "MySQL startup settings do not match: " + ", ".join(mismatch)
             + ". Check the .cnf mount and file permissions, then restart MySQL during maintenance.")

    def verify(self, report):
        if self.dry:
            log("DRY-RUN: local discovery only; Agent checks and SQL procedures will not run.")
            return
        c = self.c
        self.wait_agent()
        need(report["inventory"]["default_runtime"] == "dd-shim", "The SSI default runtime is not dd-shim.")
        for role in ("web", "api"):
            need(report["selected"][role]["runtime"] == "dd-shim", role + " has not been recreated with SSI.")
            tracer = report["php"][role]["ddtrace"]
            need(tracer and version_tuple(tracer) >= (1, 6, 0), role + " has no verified SSI tracer.")
            self.docker("exec", c[role + "_container"], "php", "-r",
                        '$s=@stream_socket_client("unix:///var/run/datadog/apm.socket",$e,$m,5);exit($s?0:1);')
            for key, val in {"DD_ENV": c["env"], "DD_SERVICE": c[role + "_service"],
                             "DD_VERSION": c["version"],
                             "DD_TRACE_AGENT_URL": "unix:///var/run/datadog/apm.socket",
                             "DD_DBM_PROPAGATION_MODE": "full" if role in c["db_apps"] else "disabled"}.items():
                # Exit code only: don't expose container environment.
                self.docker("exec", c[role + "_container"], "php", "-r",
                            'exit(getenv($argv[1])===$argv[2]?0:1);', key, val)
        self.verify_mysql_startup()
        consumers = self.mysql("SELECT COUNT(*) FROM performance_schema.setup_consumers "
                               "WHERE name IN ('events_statements_current','events_waits_current',"
                               "'events_statements_history_long') AND enabled='YES';", admin=False)
        need(consumers == "3", "Required Performance Schema consumers are not enabled.")
        for schema in ["datadog"] + c["mysql_schemas"]:
            self.mysql("CALL " + identifier(schema) + ".explain_statement('SELECT 1');", admin=False)
        check = self.docker("exec", c["agent_name"], "agent", "check", "mysql", "--json")
        try:
            parsed = json.loads(check)
        except ValueError as exc:
            raise Failure("The Agent MySQL check did not return valid JSON; review the installed Agent's output format.") from exc
        need(parsed and not has_check_error(parsed), "The Agent MySQL integration reported errors; inspect its check output locally.")
        modules = self.docker("exec", c["web_container"], report["apache"], "-M")
        need("datadog_module" in modules, "The RUM module is not loaded.")
        self.rum_connection()
        status = json.loads(self.docker("exec", c["agent_name"], "agent", "status", "-j"))
        need(isinstance(status, dict) and status, "Agent status is empty or is not a JSON object.")
        print(jdump({"basic_local_checks": "PASS", "agent_status_sections": sorted(status),
                     "feature_flags_requested": {k: c[k] for k in (
                         "runtime_security", "network_monitoring", "universal_service_monitoring")},
                     "feature_health": "REVIEW_REQUIRED: APM/logs/process/security/system-probe sections; docs/VALIDATION.md",
                     "telemetry": "PENDING: web SAPI, browser traffic, RUM-APM, APM-DBM, logs, process, runtime security, network monitoring and USM."}))

    def smoke(self):
        for key in ("apm_url", "rum_url"):
            url = self.c.get(key)
            if not url:
                log("Skipping " + key + " smoke checks: no URL configured.")
                continue
            if self.dry:
                log("DRY-RUN: GET " + key + "; no requests sent.")
                continue
            body = self.run(["curl", "--fail", "--silent", "--show-error", "--max-time",
                             str(self.c["timeout"]), url])
            if key == "rum_url":
                need(re.search(r"datadog-rum|datadoghq-browser-agent|DD_RUM", body),
                     "RUM injection markers were not found in HTML; check CSP, compression and the returned page.")
                log("rum_url HTTP smoke and RUM injection checks passed.", "OK")
            else:
                log("apm_url HTTP smoke check passed.", "OK")
        log("Verify browser sessions and trace correlation in Datadog.", "NEXT")


def mysql_version(text):
    need("mariadb" not in text.lower() and "percona" not in text.lower(), "This MySQL distribution requires a compatibility review.")
    version = version_tuple(text)
    need(version[:2] in ((5, 7), (8, 0), (8, 4)),
         "Templates support Oracle MySQL 5.7/8.0/8.4; review other versions before deployment.")
    return list(version)


def version_tuple(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    need(match is not None, "Could not parse the version.")
    return tuple(map(int, match.groups()))


def normalize_sql(value):
    # Ignore layout outside literals only: whitespace/case inside literals affects semantics.
    tokens = re.findall(r"'(?:''|[^'])*'|[^\s']+", value.strip().rstrip(";"))
    return "".join(t if t.startswith("'") else t.lower() for t in tokens)


def has_check_error(value):
    if isinstance(value, dict):
        return any((k.lower() in ("error", "errors") and bool(v)) or has_check_error(v)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(has_check_error(v) for v in value)
    return False


def run_stage(args):
    log("Datadog | " + args.stage + (" | dry-run" if args.dry_run else ""), "STEP")
    config = read_json(args.config)
    secrets = read_json(args.secrets, secret=True) if Path(args.secrets).exists() else {}
    deploy = Deployment(config, secrets, dry=args.dry_run)
    if args.stage == "discover":
        print(jdump(deploy.discover()))
        return 0
    if args.stage == "fetch-installers":
        validate(config)
        deploy.fetch()
        return 0
    report = deploy.preflight(full=args.stage != "preflight")
    if args.stage == "preflight":
        print(jdump(report))
        return 0
    actions = {
        "render": lambda: deploy.render(report),
        "agent-start": lambda: deploy.start_agent(report),
        "ssi-install": lambda: deploy.install_ssi(report),
        "dbm-export": lambda: deploy.dbm_sql(report, export=True),
        "dbm-apply": lambda: deploy.dbm_sql(report),
        "rum-install": lambda: deploy.install_rum(report),
        "rum-export": lambda: deploy.export_rum(report),
        "verify": lambda: deploy.verify(report),
        "smoke": deploy.smoke,
    }
    actions[args.stage]()
    log("Stage completed: " + args.stage, "OK")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sucofindo Datadog staged production deployment")
    parser.add_argument("stage", choices=["discover", "preflight", "fetch-installers", "render",
                                        "agent-start", "ssi-install", "dbm-export", "dbm-apply",
                                        "rum-install", "rum-export", "verify", "smoke"])
    parser.add_argument("--config", default="config/production.json")
    parser.add_argument("--secrets", default="config/secrets.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--maintenance", action="store_true", help="active maintenance window for SSI / SQL / RUM")
    args = parser.parse_args(argv)
    try:
        if args.stage in ("ssi-install", "dbm-apply", "rum-install") and not args.dry_run:
            need(args.maintenance, "This stage requires --maintenance during an approved maintenance window.")
        with deployment_lock():
            return run_stage(args)
    except (Failure, OSError, KeyError, ValueError, TypeError, IndexError, KeyboardInterrupt) as exc:
        report_error(exc, "Datadog | " + args.stage)
        return 1


if __name__ == "__main__":
    sys.exit(main())
