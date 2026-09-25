#!/usr/bin/env python3
"""Staged Sucofindo production deployment; Python >=3.8, standard library only."""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

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


class Failure(Exception):
    pass


def need(condition, message):
    if not condition:
        raise Failure(message)


def jdump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def identifier(value):
    need(isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value),
         "Identifier SQL tidak valid.")
    return chr(96) + value + chr(96)


def literal(value):
    # Always used under explicit NO_BACKSLASH_ESCAPES, never caller SQL mode.
    need(isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value),
         "Nilai SQL mengandung karakter kontrol.")
    return "'" + value.replace("'", "''") + "'"


def filled(value):
    return isinstance(value, str) and bool(value) and not re.search(r"REPLACE|CHANGE_ME|[<>]", value, re.I)


def secure_write(path, content):
    path = Path(path)
    need(not path.is_symlink(), "Menolak output symlink.")
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
    need(path.is_file() and not path.is_symlink(), "File konfigurasi tidak ditemukan / symlink.")
    if secret and os.name != "nt":
        need(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "Secret file wajib mode 0600 (chmod 600).")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise Failure("JSON konfigurasi tidak valid.") from exc
    need(isinstance(value, dict), "Konfigurasi wajib object JSON.")
    return value


def validate(c, full=False):
    required = ("web_container", "api_container", "mysql_container", "network")
    for key in required:
        need(filled(c.get(key)) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", c[key]),
             "Isi parameter eksplisit: " + key + " (lihat discover).")
    need(len({c[k] for k in required[:3]}) == 3, "Web, API, dan MySQL harus container berbeda.")
    need(isinstance(c.get("env"), str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", c["env"])
         and filled(c["env"]), "env must be a lowercase environment name, e.g. prod or lab.")
    need(type(c.get("timeout")) is int and 1 <= c["timeout"] <= 1800, "timeout harus 1..1800.")
    need(type(c.get("mysql_port")) is int and 1 <= c["mysql_port"] <= 65535, "mysql_port tidak valid.")
    for key in ("runtime_security", "network_monitoring", "universal_service_monitoring", "process_collection"):
        need(type(c.get(key)) is bool, key + " wajib boolean.")
    for key in ("output_dir", "artifact_dir", "socket_dir", "agent_run_dir", "docker_socket",
                "container_logs_dir", "mysql_data_dir", "mysql_config_target"):
        value = c.get(key, "")
        need(value.startswith("/") and value != "/" and not re.search(r"[\s:$\\]", value)
             and ".." not in value.split("/"), key + " wajib path Linux absolut tanpa spasi/$/:.")
    need(c["socket_dir"] == "/var/run/datadog", "SSI socket dir harus /var/run/datadog.")
    if not full:
        return
    for key in ("agent_name", "site", "version", "web_service", "api_service", "mysql_host",
                "db_user", "db_user_host", "rum_application_id", "rum_client_token",
                "rum_remote_configuration_id"):
        need(filled(c.get(key)) and re.fullmatch(r"[A-Za-z0-9_.%:/-]+", c[key]),
             "Parameter belum valid: " + key)
    need(c["agent_name"] not in {c[k] for k in required[:3]}, "Agent name bertabrakan.")
    need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", c["agent_name"]), "agent_name tidak valid.")
    match = re.fullmatch(r"registry\.datadoghq\.com/agent:7\.(\d+)\.(\d+)", c.get("agent_image", ""))
    need(match is not None and tuple(map(int, match.groups())) >= (76, 1),
         "Pin agent_image registry.datadoghq.com/agent:7.X.Y >=7.76.1.")
    schemas = c.get("mysql_schemas")
    need(isinstance(schemas, list) and schemas and len(set(schemas)) == len(schemas),
         "mysql_schemas wajib list unik.")
    for schema in schemas:
        need(filled(schema), "Schema masih placeholder.")
        identifier(schema)
        need(schema not in ("mysql", "sys", "performance_schema", "information_schema", "datadog"),
             "Pilih schema aplikasi, bukan schema sistem.")
    identifier(c["db_user"])
    need(set(c.get("db_apps", [])) <= {"web", "api"} and c.get("db_apps"), "db_apps wajib web dan/atau api.")
    for key in ("rum_url", "apm_url"):
        value = c.get(key, "")
        need(filled(value) and re.fullmatch(r"https?://[^@\s]+", value), key + " wajib HTTP(S) tanpa credential.")


class Runner:
    def __init__(self, timeout=60):
        self.timeout = timeout

    def run(self, args, data=None, env=None, check=True, timeout=None):
        try:
            result = subprocess.run(args, input=data, text=True, encoding="utf-8", stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout or self.timeout,
                                    env=env, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failure("Command tidak tersedia atau timeout: " + args[0]) from exc
        if check and result.returncode:
            raise Failure("Command gagal: " + args[0] + " (exit " + str(result.returncode)
                          + "); output ditahan untuk melindungi secret.")
        return result


class Deployment:
    def __init__(self, config, secrets=None, runner=None, dry=False):
        self.c = config
        self.s = secrets or {}
        self.r = runner or Runner(config.get("timeout", 60))
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
        need(platform.system() == "Linux", "Jalankan preflight pada host Ubuntu target.")
        release = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                release[key] = val.strip('"')
        need(release.get("ID") == "ubuntu" and release.get("VERSION_ID", "").split(".")[0]
             in ("20", "22", "24"), "Ubuntu belum dalam matriks SSI 20/22/24 LTS; review diperlukan.")
        need(platform.machine() in ("x86_64", "aarch64", "arm64"), "Arsitektur SSI tidak didukung.")
        for tool in ("docker", "bash", "curl", "tar", "sha256sum", "systemctl", "dpkg-query"):
            need(shutil.which(tool), "Prasyarat host tidak tersedia: " + tool)
        inventory = self.discover()
        need(not inventory["rootless"], "Paket memerlukan Docker rootful untuk SSI/host telemetry.")
        # Docker context/DOCKER_HOST may target another machine. Host mounts and SSI
        # are safe only when talking to the daemon on this host.
        endpoint = self.docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
        effective_endpoint = os.environ.get("DOCKER_HOST", endpoint)
        need(effective_endpoint == "unix://" + c["docker_socket"],
             "Docker endpoint bukan socket lokal yang dikonfigurasi; jangan jalankan SSI ke daemon remote.")
        selected = {role: self.inspect(c[role + "_container"]) for role in ("web", "api", "mysql")}
        for role, item in selected.items():
            need(item["running"], "Container " + role + " tidak running.")
            need(c["network"] in item["networks"], "Network pilihan tidak terhubung ke " + role)
        db_net = selected["mysql"]["networks"][c["network"]]
        db_aliases = set(db_net.get("Aliases") or []) | {
            c["mysql_container"], db_net.get("IPAddress"), db_net.get("GlobalIPv6Address")}
        need(c["mysql_host"] in db_aliases,
             "mysql_host bukan alias/IP database yang dipilih pada network target.")
        persistent = [m for m in selected["mysql"]["mounts"] if m["Type"] in ("volume", "bind") and m.get("RW")
                      and (c["mysql_data_dir"] == m["Destination"] or
                           c["mysql_data_dir"].startswith(m["Destination"].rstrip("/") + "/"))]
        need(persistent, "Datadir MySQL belum terbukti persistent; DBA harus memeriksa mount/datadir.")
        version = mysql_version(self.docker("exec", c["mysql_container"], "mysqld", "--version"))
        php, warnings = {}, []
        for role in ("web", "api"):
            php[role] = json.loads(self.docker("exec", c[role + "_container"], "php", "-r", PHP_PROBE))
            p = php[role]
            need(p["version"].startswith("8.1."), "PHP aktual berbeda dari target 8.1: " + role)
            need(not p["conflicts"], "Extension menghalangi SSI pada " + role)
            need(not p.get("manual_ddtrace_ini"), "Konfigurasi tracer manual terdeteksi pada " + role
                 + "; jangan instrumentasi ganda dengan SSI.")
            need(p["jit"] in ("", "0", "off", "disable", None) or
                 p["jit_buffer"] in ("", "0", None), "JIT PHP aktif; SSI tidak dipaksa.")
            if p["ddtrace"] and inventory["default_runtime"] != "dd-shim":
                raise Failure("Tracer existing tanpa SSI terverifikasi: audit instrumentation ganda diperlukan.")
            if role in c.get("db_apps", []) and "mysql" not in p["pdo"]:
                warnings.append(role + ": PDO mysql tidak tersedia; korelasi APM–DBM belum terbukti.")
            if p["mysqli"]:
                warnings.append(role + ": MySQLi tersedia; driver CodeIgniter aktual perlu konfirmasi tim.")
        apache = self.docker("exec", c["web_container"], "sh", "-c", APACHE_PROBE).splitlines()[0]
        need(re.fullmatch(r"/[A-Za-z0-9_./-]+", apache), "Path Apache tidak valid.")
        apache_v = self.docker("exec", c["web_container"], apache, "-V")
        root = re.search(r'HTTPD_ROOT="([^"]+)"', apache_v)
        need(root is not None, "Tidak dapat mendeteksi HTTPD_ROOT Apache.")
        apache_root = root.group(1)
        need(apache_root in ("/etc/apache2", "/usr/local/apache2", "/etc/httpd"),
             "HTTPD_ROOT custom memerlukan review persistence RUM.")
        conf = re.search(r'SERVER_CONFIG_FILE="([^"]+)"', apache_v)
        need(conf is not None, "SERVER_CONFIG_FILE Apache tidak ditemukan.")
        apache_config = conf.group(1)
        if not apache_config.startswith("/"):
            apache_config = apache_root + "/" + apache_config
        need(apache_config.startswith(apache_root + "/") and ".." not in apache_config.split("/"),
             "Apache config di luar root backup; review layout diperlukan.")
        self.docker("exec", c["web_container"], apache, "-t")
        if require_rum_tools:
            for tool in ("curl", "tar", "gzip", "gpg", "sh"):
                self.docker("exec", c["web_container"], "sh", "-c", 'command -v "$1"', "sh", tool)
        for role in ("web", "api"):
            self.docker("exec", c[role + "_container"], "php", "-r",
                        '$s=@fsockopen($argv[1],(int)$argv[2],$e,$m,5); exit($s?0:1);',
                        c["mysql_host"], str(c["mysql_port"]))
        warnings.append("CLI PHP bukan bukti SAPI web; cek runtime HTTP, driver CodeIgniter, dan traces setelah handoff.")
        if c["network_monitoring"] or c["universal_service_monitoring"] or c["runtime_security"]:
            kernel = version_tuple(inventory["kernel"])
            need(kernel >= (4, 14, 0), "Fitur system-probe memerlukan kernel kompatibel >=4.14.")
            need(Path("/sys/kernel/debug").exists(), "debugfs host tidak tersedia.")
        return {"inventory": inventory, "ubuntu": release["VERSION_ID"], "mysql_version": version,
                "mysql_persistence": [{"type": m["Type"], "target": m["Destination"]} for m in persistent],
                "selected": selected, "php": php, "apache": apache, "apache_root": apache_root,
                "apache_config": apache_config,
                "warnings": warnings, "telemetry": "PENDING_BROWSER_AND_APPLICATION_TRAFFIC"}

    def secret(self, key):
        value = self.s.get(key)
        need(isinstance(value, str) and value and "REPLACE" not in value
             and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value), "Secret belum valid: " + key)
        return value

    def service(self, report, role):
        label = report["selected"][role]["service"]
        explicit = self.c.get(role + "_compose_service")
        need(not (label and explicit and label != explicit),
             "Mapping service eksplisit tidak cocok dengan Compose label: " + role)
        value = label or explicit
        need(value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value),
             "Label Compose kosong; isi " + role + "_compose_service.")
        return value

    def render(self, report):
        c = self.c
        need(len({self.service(report, role) for role in ("web", "api", "mysql")}) == 3,
             "Mapping Compose memiliki service duplikat; review multi-project mapping.")
        projects = {item.get("project") for item in report["selected"].values() if item.get("project")}
        need(len(projects) <= 1, "Container berasal dari project Compose berbeda; snippet harus dipisahkan tim.")
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
            print("DRY-RUN: render " + ", ".join(artifacts) + "; tidak ada file ditulis.")
            return
        for name in ("mysql.d/conf.yaml", "agent-spec.json"):
            path = self.out / name
            need(not path.exists() or path.read_text(encoding="utf-8") == artifacts[name],
                 "Config Agent berubah; gunakan output_dir baru dan review migrasi Agent.")
        for name, body in artifacts.items():
            path = self.out / name
            need(not path.is_symlink(), "Menolak output symlink.")
            # Preserve inode for bind-mounted files on unchanged reruns.
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                secure_write(path, body)
            if name == "99-datadog.cnf":
                # No secrets: mysqld runs as a non-root user and must read this
                # bind mount. Repair older renders without replacing their inode.
                path.chmod(0o644)
        print("HANDOFF: review application.override.json. Tim Sucofindo memasang mount/config,"
              " lalu recreate DB/aplikasi sendiri. Lanjutkan tahap berikut setelah handoff.")

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
        need(config_path.is_file(), "Jalankan render dahulu.")
        spec_hash = hashlib.sha256((jdump(spec) + config_path.read_text(encoding="utf-8")
                                   + self.secret("api_key")).encode()).hexdigest()
        inv = report["inventory"]
        candidates = inv["agent_candidates"]
        need(not inv["host_agent_active"], "Agent host aktif; review satu Agent per host sebelum lanjut.")
        if candidates:
            need(candidates == [self.c["agent_name"]], "Ada Agent existing lain; tidak dibuat Agent kedua.")
            existing = self.inspect(candidates[0])
            need(existing["managed"] == "true" and existing["spec"] == spec_hash,
                 "Agent existing bukan milik paket atau spec berubah. Tidak diganti otomatis.")
            need(existing["running"], "Agent managed berhenti; tim periksa lalu start eksplisit.")
            self.wait_agent()
            print("Agent managed sudah running dengan spec sama; skip create.")
            return
        self.secret("api_key")
        for mount in spec["mounts"]:
            source = mount.split(":")[0]
            if source not in (self.c["socket_dir"], self.c["agent_run_dir"]):
                need(Path(source).exists(), "Sumber mount host tidak ada: " + source)
        if self.dry:
            print("DRY-RUN: create Agent terpisah; environment secret tidak ditampilkan.")
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
        print("Agent local health OK; telemetry Datadog masih perlu diverifikasi.")

    def wait_agent(self):
        deadline = time.monotonic() + self.c["timeout"]
        while True:
            result = self.r.run(["docker", "exec", self.c["agent_name"], "agent", "health"], check=False)
            if result.returncode == 0:
                return
            need(time.monotonic() < deadline, "Timeout Agent health.")
            time.sleep(2)

    def fetch(self):
        if self.dry:
            print("DRY-RUN: download dua installer ke artifact_dir untuk review; tidak download.")
            return
        for name, url in (("ssi.sh", SSI_URL), ("rum.sh", RUM_URL)):
            path = Path(self.c["artifact_dir"]) / name
            if path.exists():
                print(name + " existing SHA256=" + hashlib.sha256(path.read_bytes()).hexdigest())
                continue
            try:
                with urllib.request.urlopen(url, timeout=self.c["timeout"]) as response:
                    body = response.read(4 * 1024 * 1024 + 1)
                need(len(body) <= 4 * 1024 * 1024 and body.startswith(b"#!"), "Artifact bukan script.")
                secure_write(path, body.decode("utf-8"))
            except (OSError, UnicodeError) as exc:
                raise Failure("Download installer gagal; tidak ada installer dieksekusi.") from exc
            print(name + " SHA256=" + hashlib.sha256(path.read_bytes()).hexdigest())
        print("Review script dan download transitif. Isi hash di production.json sebelum install.")

    def artifact(self, name):
        path = Path(self.c["artifact_dir"]) / (name + ".sh")
        expected = self.c.get(name + "_sha256", "")
        need(re.fullmatch(r"[a-fA-F0-9]{64}", expected) and len(set(expected)) > 1,
             "Isi checksum reviewed: " + name + "_sha256.")
        need(path.is_file() and not path.is_symlink(), "Artifact belum tersedia: " + name)
        need(hashlib.sha256(path.read_bytes()).hexdigest() == expected.lower(), "Checksum installer berbeda.")
        return path

    def install_ssi(self, report):
        inventory = report["inventory"]
        if inventory["default_runtime"] == "dd-shim":
            need(any("datadog-apm-library-php" in x for x in inventory["ssi_packages"]),
                 "dd-shim ada tetapi paket PHP SSI belum terbukti; review manual.")
            print("SSI Docker existing terdeteksi; skip instalasi. Verifikasi container setelah recreate.")
            return
        need(not inventory["ssi_packages"], "Instalasi SSI parsial ditemukan; review dahulu.")
        path = self.artifact("ssi")
        if self.dry:
            print("DRY-RUN: SSI host docker, php:1, DD_NO_AGENT_INSTALL=true; handoff recreate.")
            return
        need(os.geteuid() == 0, "ssi-install harus root di maintenance window.")
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
             "Installer selesai tetapi default runtime belum dd-shim.")
        print("HANDOFF SSI: tim Sucofindo recreate aplikasi. Restart saja tidak menerapkan SSI.")

    def mysql(self, sql, admin=True):
        """Password via stdin, never Docker argv or container environment config."""
        password = self.secret("admin_password" if admin else "db_password")
        user = self.c["admin_user" if admin else "db_user"]
        script = ('IFS= read -r MYSQL_PWD; export MYSQL_PWD; '
                  'exec mysql --protocol=tcp --host=127.0.0.1 --port="$1" --user="$2" '
                  '--batch --raw --skip-column-names --binary-mode --default-character-set=utf8mb4 '
                  '--init-command="SET SESSION sql_mode=\'NO_BACKSLASH_ESCAPES\'"')
        return self.docker("exec", "-i", self.c["mysql_container"], "sh", "-c", script,
                           "sh", str(self.c["mysql_port"]), user, data=password + "\n" + SQL_MODE + sql)

    def procedures(self):
        result = {}
        explain = (ROOT / "datadog/templates/explain.sql").read_text().strip()
        for schema in ["datadog"] + self.c["mysql_schemas"]:
            result[(schema, "explain_statement")] = explain.format(schema=identifier(schema))
        result[("datadog", "enable_events_statements_consumers")] = (
            ROOT / "datadog/templates/consumers.sql").read_text().strip()
        return result

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
                print("DRY-RUN: export SQL DBA berisi secret; tidak ditulis/ditampilkan.")
            else:
                secure_write(self.out / "dbm-dba-review.sql", "".join(chunks))
                print("HANDOFF DBA: dbm-dba-review.sql (0600). Review existing objects sebelum eksekusi.")
            return
        actual = self.mysql("SELECT VERSION();")
        need(mysql_version(actual)[:2] == report["mysql_version"][:2], "Versi server berubah.")
        datadir = self.mysql("SELECT @@datadir;").rstrip("/")
        need(datadir == c["mysql_data_dir"].rstrip("/"), "Datadir server berbeda dari mapping.")
        for schema in c["mysql_schemas"]:
            need(self.mysql("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name="
                            + literal(schema) + ";") == "1", "Schema aplikasi tidak ditemukan.")
        exists = self.mysql("SELECT COUNT(*) FROM mysql.user WHERE user=" + literal(c["db_user"])
                            + " AND host=" + literal(c["db_user_host"]) + ";") == "1"
        if exists:
            need(self.mysql("SELECT 1;", admin=False) == "1", "Kredensial user DBM existing berbeda.")
            need(self.mysql("SELECT CURRENT_USER();", admin=False) == c["db_user"] + "@" + c["db_user_host"],
                 "Koneksi DBM cocok ke account host lain; review DBA.")
            limit = self.mysql("SELECT max_user_connections FROM mysql.user WHERE user="
                               + literal(c["db_user"]) + " AND host=" + literal(c["db_user_host"]) + ";")
            need(limit == "5", "Account existing memiliki limit berbeda; DBA review, tidak diubah otomatis.")
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
                     "Procedure existing berbeda/tidak dapat dibaca: " + schema + "." + name)
                params = self.mysql(
                    "SELECT CONCAT(parameter_mode,':',data_type) FROM information_schema.parameters "
                    "WHERE specific_schema=" + literal(schema) + " AND specific_name=" + literal(name)
                    + " ORDER BY ordinal_position;")
                need(params == ("IN:text" if name == "explain_statement" else ""),
                     "Signature procedure existing berbeda: " + schema + "." + name)
            else:
                pending.append(ddl)
        if self.dry:
            print("DRY-RUN: DBM checks OK; user existing=" + str(exists)
                  + "; procedure baru=" + str(len(pending)) + "; tidak ada SQL mutasi.")
            return
        self.mysql(("" if exists else create) + grants)
        for ddl in pending:
            self.mysql("DELIMITER $$\n" + ddl + "$$\nDELIMITER ;\n")
        for schema, name in routines:
            self.mysql("GRANT EXECUTE ON PROCEDURE " + identifier(schema) + "." + name
                       + " TO " + account + ";")
        self.mysql("CALL datadog.enable_events_statements_consumers();")
        print("SQL DBM selesai; tidak restart MySQL. Lanjut verify setelah startup config diterapkan.")

    def rum_connection(self):
        uri = "http://" + self.c["agent_name"] + ":8126"
        self.docker("exec", self.c["web_container"], "curl", "--fail", "--silent",
                    "--show-error", "--max-time", "10", uri + "/info")
        return uri

    def install_rum(self, report):
        c = self.c
        path = self.artifact("rum")
        uri = self.rum_connection()
        modules = self.docker("exec", c["web_container"], report["apache"], "-M")
        if "datadog_module" in modules:
            print("Modul RUM existing: skip installer. Gunakan verify dan review config production.")
            return
        if self.dry:
            print("DRY-RUN: backup Apache, installer RUM httpd, config test, graceful reload, export persistence.")
            return
        backup = self.out / ("rum-before-" + str(time.time_ns()))
        backup.mkdir(parents=True, mode=0o700)
        self.docker("cp", c["web_container"] + ":" + report["apache_root"], str(backup / "apache"))
        secure_write(backup / "manifest.json", jdump({
            "container_id": report["selected"]["web"]["id"],
            "apache_root": report["apache_root"], "apache_config": report["apache_config"]}))
        work = self.docker("exec", "-u", "0", c["web_container"], "mktemp", "-d", "/tmp/dd-rum.XXXXXXXX")
        need(re.fullmatch(r"/tmp/dd-rum\.[A-Za-z0-9]+", work), "Temporary path RUM tidak valid.")
        self.docker("cp", str(path), c["web_container"] + ":" + work + "/installer.sh")
        help_text = self.docker("exec", "-u", "0", "-w", work, c["web_container"],
                                "sh", "./installer.sh", "--help", timeout=600,
                                include_stderr=True)
        for flag in ("proxyKind", "appId", "site", "clientToken", "remoteConfigurationId", "agentUri"):
            need(flag in help_text, "Configurator RUM tidak mengiklankan flag " + flag
                 + "; hentikan, review installer sebelum konfigurasi Apache.")
        self.docker("exec", "-u", "0", "-w", work, c["web_container"], "sh", "./installer.sh",
                    "--proxyKind", "httpd", "--appId", c["rum_application_id"],
                    "--site", c["site"], "--clientToken", c["rum_client_token"],
                    "--remoteConfigurationId", c["rum_remote_configuration_id"],
                    "--agentUri", uri, timeout=600)
        # The Apache module's own APM tracing is separate from SSI PHP. RUM only here.
        self.docker("exec", "-i", "-u", "0", c["web_container"], "sh", "-c",
                    'cat >> "$1"', "sh", report["apache_config"],
                    data="\n<IfModule datadog_module>\n  DatadogTracing Off\n</IfModule>\n")
        try:
            self.docker("exec", c["web_container"], report["apache"], "-t")
        except Failure as exc:
            raise Failure("Config test gagal; tidak reload. Restore Apache dari " + str(backup)) from exc
        self.docker("exec", c["web_container"], report["apache"], "-k", "graceful")
        return self.export_rum(report)

    def export_rum(self, report):
        if self.dry:
            print("DRY-RUN: export Apache + /opt/datadog-httpd dan manifest untuk persistence.")
            return
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
                     "# Tim: restore USER asli; base image/distro/Apache ABI harus sama.\n")
        secure_write(target / "persistence.override.example.json", jdump({"services": {
            self.service(report, "web"): {"volumes": [
                str(target / "module") + ":/opt/datadog-httpd:ro",
                str(target / "apache") + ":" + report["apache_root"] + ":ro"]}}}))
        print("HANDOFF RUM persistence: " + str(target)
              + ". Review diff/config/assets lalu masukkan ke image web tim; uji setelah recreate.")
        return target

    def verify_mysql_startup(self, admin=False):
        names = ("performance_schema", "max_digest_length", "performance_schema_max_digest_length",
                 "performance_schema_max_sql_text_length")
        result = self.mysql("SELECT " + ",".join("@@" + name for name in names) + ";", admin=admin)
        values = result.split("\t")
        need(len(values) == 4 and all(re.fullmatch(r"[0-9]+", value) for value in values),
             "Tidak dapat membaca startup settings MySQL.")
        expected = ["1", "4096", "4096", "4096"]
        mismatch = [name + "=" + actual + " (expected " + wanted + ")"
                    for name, actual, wanted in zip(names, values, expected) if actual != wanted]
        need(not mismatch, "Startup settings MySQL belum sesuai: " + ", ".join(mismatch)
             + ". Periksa mount/permission .cnf dan restart database.")

    def verify(self, report):
        if self.dry:
            print("DRY-RUN: discovery lokal saja; Agent check dan SQL procedure tidak dijalankan.")
            return
        c = self.c
        self.wait_agent()
        need(report["inventory"]["default_runtime"] == "dd-shim", "SSI default runtime bukan dd-shim.")
        for role in ("web", "api"):
            need(report["selected"][role]["runtime"] == "dd-shim", role + " belum direcreate dengan SSI.")
            tracer = report["php"][role]["ddtrace"]
            need(tracer and version_tuple(tracer) >= (1, 6, 0), role + " tracer SSI belum terbukti.")
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
        need(consumers == "3", "Performance Schema consumers belum enabled.")
        for schema in ["datadog"] + c["mysql_schemas"]:
            self.mysql("CALL " + identifier(schema) + ".explain_statement('SELECT 1');", admin=False)
        check = self.docker("exec", c["agent_name"], "agent", "check", "mysql", "--json")
        try:
            parsed = json.loads(check)
        except ValueError as exc:
            raise Failure("Agent mysql check bukan JSON; review format versi Agent.") from exc
        need(parsed and not has_check_error(parsed), "Agent MySQL integration melaporkan error.")
        modules = self.docker("exec", c["web_container"], report["apache"], "-M")
        need("datadog_module" in modules, "Modul RUM belum loaded.")
        self.rum_connection()
        status = json.loads(self.docker("exec", c["agent_name"], "agent", "status", "-j"))
        need(isinstance(status, dict) and status, "Agent status kosong.")
        print(jdump({"basic_local_checks": "PASS", "agent_status_sections": sorted(status),
                     "feature_flags_requested": {k: c[k] for k in (
                         "runtime_security", "network_monitoring", "universal_service_monitoring")},
                     "feature_health": "REVIEW_REQUIRED: APM/logs/process/security/system-probe sections; docs/VALIDATION.md",
                     "telemetry": "PENDING: web SAPI, traffic browser, RUM–APM, APM–DBM, logs, fitur opsional."}))

    def smoke(self):
        if self.dry:
            print("DRY-RUN: GET endpoint aplikasi/browser; tidak mengirim request.")
            return
        for key in ("apm_url", "rum_url"):
            body = self.run(["curl", "--fail", "--silent", "--show-error", "--max-time",
                             str(self.c["timeout"]), self.c[key]])
            if key == "rum_url":
                need(re.search(r"datadog-rum|datadoghq-browser-agent|DD_RUM", body),
                     "HTML belum membuktikan injection RUM (cek CSP/compression/HTML).")
        print("Smoke HTTP + marker injection OK. Browser/session dan korelasi trace perlu dicek di UI.")


def mysql_version(text):
    need("mariadb" not in text.lower() and "percona" not in text.lower(), "Distribusi MySQL perlu review.")
    version = version_tuple(text)
    need(version[:2] in ((5, 7), (8, 0), (8, 4)),
         "Template mendukung Oracle MySQL 5.7/8.0/8.4; versi lain memerlukan review.")
    return list(version)


def version_tuple(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    need(match is not None, "Tidak dapat membaca versi.")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sucofindo Datadog staged production deployment")
    parser.add_argument("stage", choices=["discover", "preflight", "fetch-installers", "render",
                                        "agent-start", "ssi-install", "dbm-export", "dbm-apply",
                                        "rum-install", "rum-export", "verify", "smoke"])
    parser.add_argument("--config", default="config/production.json")
    parser.add_argument("--secrets", default="config/secrets.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--maintenance", action="store_true", help="window aktif untuk SSI / SQL / RUM")
    args = parser.parse_args(argv)
    try:
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
        if args.stage in ("ssi-install", "dbm-apply", "rum-install") and not args.dry_run:
            need(args.maintenance, "Tahap ini membutuhkan --maintenance pada window yang disetujui.")
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
        return 0
    except (Failure, OSError, KeyError, ValueError, TypeError, IndexError) as exc:
        print("ERROR tahap " + args.stage + ": " + (str(exc) if isinstance(exc, Failure) else
                          "Konfigurasi/data lokal tidak valid; periksa field/file."), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
