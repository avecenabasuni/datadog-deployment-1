"""English CLI progress and bounded, credential-redacted error diagnostics."""
import json
import logging
from pathlib import Path
import re
import shlex
import sys
from urllib.parse import quote, quote_plus


class Failure(Exception):
    def __init__(self, message, *, details=None, hint=None, command=None, exit_code=None):
        super().__init__(message)
        self.details = details
        self.hint = hint
        self.command = command
        self.exit_code = exit_code


class Redactor:
    def __init__(self, values=()):
        self.values = set()
        self.add(values)

    def add(self, values):
        for value in values:
            if isinstance(value, str) and value:
                self.values.update((value, quote(value, safe=""), quote_plus(value),
                                    json.dumps(value)[1:-1], value.replace("'", "''"), shlex.quote(value)))

    def clean(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = str(text or "")
        text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
                      "[REDACTED PRIVATE KEY]", text, flags=re.S)
        text = re.sub(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=][^\r\n]*",
                      r"\1: [REDACTED]", text)
        text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", text)
        text = re.sub(r"(?im)(^[ \t]*[\w.-]*(?:password|secret|token|api[_-]?key)[\w.-]*\s*:\s*)[|>][-+]?[^\n]*\n(?:[ \t]+[^\n]*(?:\n|$))+",
                      r"\1[REDACTED]\n", text)
        text = re.sub(r'''(?ix)(["']?[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|db_pass|credential)[\w.-]*["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)''',
                      r"\1[REDACTED]", text)
        text = re.sub(r"(?i)(--(?:password|token|api-key|secret)\s+)(\S+)", r"\1[REDACTED]", text)
        text = re.sub(r'''(?i)(identified\s+by\s+)(?:'[^']*(?:''[^']*)*'|"[^"]*(?:""[^"]*)*"|\S+)''', r"\1[REDACTED]", text)
        text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)
        text = re.sub(r"(https?://[^\s?#]+)\?[^\s]*", r"\1?[REDACTED]", text)
        # Strip terminal control sequences and carriage-return log spoofing.
        text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


_redactor = Redactor()


def register_secrets(values):
    _redactor.add(values)


def log(message, level="INFO", **unused):
    """All human output goes to stderr; stdout remains available for JSON."""
    prefixes = {"DRY-RUN:": "PLAN", "PLAN:": "PLAN", "HANDOFF:": "NEXT",
                "HANDOFF SSI:": "NEXT", "HANDOFF DBA:": "NEXT", "HANDOFF RUM persistence:": "NEXT"}
    for prefix, label in prefixes.items():
        if message.startswith(prefix):
            level, message = label, message[len(prefix):].strip()
            break
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(label)-5s] %(message)s", datefmt="%H:%M:%S"))
    logger = logging.Logger("eminerba.cli", logging.INFO)
    logger.addHandler(handler)
    for line in _redactor.clean(message).splitlines():
        logger.info(line, extra={"label": level})


def command_label(args):
    """Describe the operation without echoing shell code, SQL, URLs or env values."""
    args = list(args)
    program = Path(str(args[0])).name
    if program in ("docker", "docker-compose"):
        words = [program]
        for arg in args[1:]:
            if arg in {"compose", "exec", "inspect", "image", "volume", "network", "context", "info",
                       "ps", "run", "build", "up", "config", "version", "cp", "tag", "ls"}:
                words.append(arg)
            else:
                break
        if len(args) > 1 and args[1] == "exec":
            index = 2
            while index < len(args) and str(args[index]).startswith("-"):
                index += 2 if args[index] in ("-u", "-w", "-e", "--user", "--workdir", "--env") else 1
            if index < len(args) and re.fullmatch(r"[A-Za-z0-9_.-]+", str(args[index])):
                words += [str(args[index])]
        if program == "docker-compose" or args[1:2] == ["compose"]:
            # Global Compose flags may precede the subcommand.
            for arg in args[2:] if program == "docker" else args[1:]:
                if arg in ("up", "config", "build", "version", "pull", "down", "stop") and arg not in words:
                    words.append(arg)
                    break
        return " ".join(words)
    if program in ("bash", "sh") and len(args) > 1 and not str(args[1]).startswith("-"):
        return program + " " + Path(args[1]).name
    return program


def next_step(text, command=""):
    value = text.lower()
    if "permission denied" in value or "access is denied" in value:
        return "Check the reported file/socket permissions and run the deployment command with sudo."
    if "cannot connect" in value or "is the docker daemon running" in value or "docker api" in value:
        return "Run sudo systemctl status docker and sudo docker info; verify the selected Docker context."
    if "access denied for user" in value:
        return "Check the MySQL username, password and grants in the local secrets/configuration files."
    if "unknown database" in value or "schema" in value:
        return "Compare mysql_schemas with the databases on the target server; do not create production schemas blindly."
    if "no such container" in value or "not running" in value:
        return "Run sudo docker ps -a and check the target container's status and configured name."
    if "address already in use" in value or "port is already allocated" in value:
        return "Check the published ports and existing containers; resolve the conflict before recreating this service."
    if "no such file" in value or "not found" in value or "file is missing" in value:
        return "Check the reported path or executable; run the documented prepare/prerequisite steps if needed."
    if "checksum" in value:
        return "Review the downloaded installer and record its approved SHA-256 hash before retrying."
    if "timeout" in value or "timed out" in value or command == "curl":
        return "Check service health, DNS/network access and the configured read-only endpoint before retrying."
    return "Fix the reported cause, then rerun --dry-run before retrying a maintenance operation."


def command_error(args, result, redactor=None):
    redactor = redactor or _redactor
    details = redactor.clean(result.stderr)
    # Stdout can contain complete Compose models, SQL and environment secrets.
    # Only use a short error line when stderr is absent, never structured dumps.
    if not details.strip() and not any(word in args for word in ("config", "inspect")):
        details = "\n".join(line for line in redactor.clean(result.stdout).splitlines()
                            if re.match(r"(?i)^\s*(?:error\b|fatal\b|failed\b)", line))
    details = "\n".join(details.strip().splitlines()[-12:])[-4000:] or "The command did not provide an error diagnostic on stderr."
    label = command_label(args)
    return Failure("Command failed.", command=label, exit_code=result.returncode,
                   details=details, hint=next_step(details, label))


def failure_context(message, previous):
    return Failure(message, details=getattr(previous, "details", None),
                   hint=getattr(previous, "hint", None), command=getattr(previous, "command", None),
                   exit_code=getattr(previous, "exit_code", None))


def report_error(error, stage):
    if isinstance(error, Failure):
        message = str(error)
    elif isinstance(error, KeyboardInterrupt):
        message = "Interrupted by the operator. Inspect partial changes before retrying."
    elif isinstance(error, OSError):
        message = "{}: {}{}".format(type(error).__name__, error.strerror or "Operating system error",
                                    " (" + str(error.filename) + ")" if error.filename else "")
    elif isinstance(error, KeyError):
        message = "Missing configuration/data field: " + str(error.args[0])
    else:
        message = type(error).__name__ + ": invalid configuration or unexpected command response. Check the current stage's inputs."
    log(stage + " | " + message, "ERROR")
    diagnostic = error
    for _ in range(5):
        if getattr(diagnostic, "details", None) or not isinstance(diagnostic.__cause__, Failure):
            break
        diagnostic = diagnostic.__cause__
    if isinstance(diagnostic, Failure):
        if diagnostic.command:
            log("Command: " + diagnostic.command + (" | exit code: " + str(diagnostic.exit_code) if diagnostic.exit_code is not None else ""), "ERROR")
        if diagnostic.details:
            log(diagnostic.details, "DETAIL")
    log(getattr(error, "hint", None) or getattr(diagnostic, "hint", None) or next_step(message), "NEXT")
    log("Stopped. No automatic rollback or volume deletion was performed.", "STOP")
