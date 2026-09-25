"""Explicit application recovery; never undo database contents or host installers."""
import json
from pathlib import Path
import re

import datadog_deploy as d
from eminerba_production import MAPPING, LAB_PROJECT, digest, mount_source, validate_lab_fixture


def snapshot(flow, report):
    path = flow.app.out / "recovery.json"
    if path.exists():
        saved = d.read_json(path, secret=True)
        d.need(saved["compose_digest"] == flow.base_digest and
               saved["compose_file"] == str(flow.compose_file) and saved["project"] == flow.project and
               saved.get("version") == 1 and saved["env"] == flow.c["env"] and
               saved["mysql"] == {key: flow.c[key] for key in ("mysql_port", "mysql_data_dir", "admin_user")} and
               digest(saved["mounts"]) == digest(flow.preserved_mounts),
               "Recovery baseline differs; review before deployment.")
        d.need(set(saved["images"]) == {"web", "api", "db"}, "Incomplete recovery images.")
        for image in saved["images"].values():
            d.need(re.fullmatch(r"sha256:[a-f0-9]{64}", image) and
                   json.loads(flow.app.docker("image", "inspect", "--format", "{{json .Id}}", image)) == image,
                   "Original recovery image unavailable; restore it before deployment.")
        return
    images = {}
    for role, (service, container) in MAPPING.items():
        original = flow.state.get("original_images", {}).get(service)
        image = json.loads(flow.app.docker("image", "inspect", "--format", "{{json .Id}}", original)) if original else \
            json.loads(flow.app.docker("inspect", "--format", "{{json .Image}}", container))
        d.need(re.fullmatch(r"sha256:[a-f0-9]{64}", image), "Cannot record original recovery image: " + service)
        # Protect images from dangling-image cleanup; snapshot stores immutable IDs.
        flow.app.docker("image", "tag", image, "eminerba-observability/" + service + "-recovery:" + image[7:])
        images[service] = image
    d.secure_write(path, d.jdump({"version": 1, "env": flow.c["env"], "project": flow.project,
        "compose_file": str(flow.compose_file), "compose_digest": flow.base_digest,
        "mysql": {key: flow.c[key] for key in ("mysql_port", "mysql_data_dir", "admin_user")},
        "images": images, "mounts": flow.preserved_mounts}))


def verify_recovered(flow, saved, role):
    service, container = MAPPING[role]
    app = flow.app
    item = app.inspect(container)
    d.need(item["running"] and item["runtime"] == "runc", "Recovered container/runtime is not ready: " + role)
    actual = {m["Destination"]: [m["Type"], m.get("Name") if m["Type"] == "volume" else m["Source"]]
              for m in item["mounts"]}
    d.need(actual == saved["mounts"][service], "Recovered mounts differ: " + role)
    d.need(json.loads(app.docker("inspect", "--format", "{{json .Image}}", container)) == saved["images"][service],
           "Recovered image differs: " + role)
    if role != "mysql":
        app.docker("exec", container, "apache2ctl", "-t")


def rollback(flow, dry=False):
    app = flow.app
    flow.step("validate application recovery baseline")
    saved = d.read_json(app.out / "recovery.json", secret=True)
    d.need(saved.get("version") == 1 and saved["env"] == ("lab" if flow.lab else "prod") == flow.c["env"],
           "Recovery profile differs from configuration.")
    d.need(saved["compose_file"] == str(flow.compose_file), "Recovery Compose path differs.")
    d.need(saved["mysql"] == {key: flow.c[key] for key in ("mysql_port", "mysql_data_dir", "admin_user")},
           "Recovery MySQL settings changed.")
    app.secret("admin_password")
    if flow.lab:
        validate_lab_fixture(flow.compose_file)
        d.need(saved["project"] == LAB_PROJECT, "Recovery project is not the lab.")
    endpoint = app.docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    d.need(d.docker_endpoint(endpoint) == "unix:///var/run/docker.sock",
           "Recovery requires the local Docker socket.")
    security = json.loads(app.docker("info", "--format", "{{json .SecurityOptions}}")) or []
    d.need(not any("rootless" in value for value in security), "Recovery requires rootful Docker.")
    runtimes = json.loads(app.docker("info", "--format", "{{json .Runtimes}}"))
    d.need("runc" in runtimes, "Recovery requires the runc runtime.")
    flow.project = saved["project"]
    flow.select_backend()
    model = flow.model()
    d.need(digest(model) == saved["compose_digest"], "Compose/env changed since baseline; manual recovery review required.")
    d.need(set(saved["images"]) == set(saved["mounts"]) == {"web", "api", "db"}, "Incomplete recovery baseline.")
    names = app.docker("ps", "-a", "--format", "{{.Names}}").splitlines()
    for role, (service, container) in MAPPING.items():
        d.need(flow.c[role + "_container"] == container, "Recovery container mapping changed.")
        if container in names:
            item = app.inspect(container)
            d.need(item["project"] == flow.project and item["service"] == service,
                   "Container belongs to another deployment: " + container)
            sources = json.loads(app.docker("inspect", "--format",
                '{{json (index .Config.Labels "com.docker.compose.project.config_files")}}', container))
            files = {str(Path(value).resolve()) for value in (sources or "").split(",")}
            d.need(str(flow.compose_file) in files and files <= {str(flow.compose_file),
                   str(app.out / "production.override.json"), str(app.out / "rollback.override.json")},
                   "Container Compose sources differ; review recovery: " + container)
            actual = {m["Destination"]: [m["Type"], m.get("Name") if m["Type"] == "volume" else m["Source"]]
                      for m in item["mounts"]}
            d.need(all(actual.get(target) == source for target, source in saved["mounts"][service].items()),
                   "Live recovery mount differs: " + service)
            managed = ({flow.c["mysql_config_target"]} if role == "mysql" else {flow.c["socket_dir"]})
            if role == "web":
                managed.update(("/etc/apache2", "/usr/local/apache2", "/etc/httpd", "/opt/datadog-httpd"))
            d.need(not set(actual) - set(saved["mounts"][service]) - managed,
                   "Unknown live mount; review recovery: " + service)
        image = saved["images"][service]
        d.need(re.fullmatch(r"sha256:[a-f0-9]{64}", image), "Invalid recovery image ID.")
        d.need(json.loads(app.docker("image", "inspect", "--format", "{{json .Id}}", image)) == image,
               "Original recovery image unavailable: " + service)
        expected = dict(mount_source(model, flow.project, mount, flow.directory)
                        for mount in model["services"][service].get("volumes", []))
        d.need({target: list(source) for target, source in expected.items()} == saved["mounts"][service],
               "Recovery mounts differ from baseline: " + service)
        for kind, source in expected.values():
            if kind == "volume":
                app.docker("volume", "inspect", "--format", "{{.Name}}", source)
            else:
                d.need(Path(source).exists(), "Recovery bind source is missing; refusing empty replacement.")
    if dry:
        print("PLAN: recreate db -> wait SQL -> api/web using original image IDs, runc and original mounts; disable tracing.")
        print("Agent, host SSI, database contents and DBM SQL objects remain. No changes made.")
        return
    overlay = {"services": {service: {"image": image, "runtime": "runc", "environment": {
        "DD_INSTRUMENT_SERVICE_WITH_APM": "false", "DD_TRACE_ENABLED": "false"}}
        for service, image in saved["images"].items()}}
    if model.get("version"):
        overlay["version"] = model["version"]
    flow.override = app.out / "rollback.override.json"
    d.secure_write(flow.override, d.jdump(overlay))
    flow.base_digest = saved["compose_digest"]
    flow.preserved_mounts = {service: {target: tuple(source) for target, source in mounts.items()}
                             for service, mounts in saved["mounts"].items()}
    flow.journal_enabled = True
    flow.step("rollback database container with original volume")
    flow.recreate("db")
    flow.wait_database()
    verify_recovered(flow, saved, "mysql")
    flow.step("rollback API and web containers")
    for role in ("api", "web"):
        flow.recreate(role)
        verify_recovered(flow, saved, role)
    flow.record_status("application_rollback_completed")
    print("APPLICATION ROLLBACK COMPLETED. Check business endpoints manually; Agent, SSI and DBM objects remain.")
    print("Use this override for subsequent uninstrumented operations: " + str(flow.override))
