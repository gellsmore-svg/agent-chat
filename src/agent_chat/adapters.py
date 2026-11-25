import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .protocol import BACKENDS, INTENTS, canonical, contains_secret, number, parse_json

SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "intent": {"type": "string", "enum": sorted(INTENTS)},
        **{field: {"type": "array", "items": {"type": "string"}}
           for field in ("decisions", "open_questions", "actions")},
    },
    "required": ["message", "intent", "decisions", "open_questions", "actions"],
    "additionalProperties": False,
}
OUTPUT_LIMIT = 2 * 1024 * 1024


class AdapterError(Exception):
    def __init__(self, message, *, retry=False, metadata=None):
        super().__init__(message)
        self.retry = retry
        self.metadata = metadata or {}


@dataclass(frozen=True)
class Backend:
    name: str
    executable: str
    version: str
    model: str | None = None
    flags: frozenset = frozenset()


CAPABILITIES = {
    "codex": {"--json", "--output-schema", "--sandbox", "--skip-git-repo-check", "--ephemeral",
              "--ignore-user-config", "--ask-for-approval", "--disable", "--model"},
    "grok": {"--prompt-file", "--output-format", "--permission-mode", "--sandbox", "--tools",
             "--deny", "--disable-web-search", "--no-subagents", "--no-plan", "--model", "--verbatim"},
    "claude": {"--print", "--output-format", "--json-schema", "--tools", "--strict-mcp-config",
               "--mcp-config", "--setting-sources", "--settings", "--no-session-persistence",
               "--permission-mode", "--disallowedTools", "--model"},
}


def child_env(name):
    environment = os.environ.copy()
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "ANTHROPIC_LOG"):
        environment.pop(key, None)
    if name == "grok":
        environment["GROK_DISABLE_AUTOUPDATER"] = "1"
        for vendor in ("CLAUDE", "CURSOR"):
            environment[f"GROK_{vendor}_HOOKS_ENABLED"] = "0"
    return environment


def probe(executable, *arguments):
    with tempfile.TemporaryDirectory(prefix="agent-chat-check-") as directory:
        try:
            return subprocess.run(
                [executable, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=directory, env=child_env("grok"), timeout=8, check=False, text=True, errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AdapterError("Could not run CLI capability probe; verify the native executable") from None


def discover(name, executable=None, model=None):
    if name not in BACKENDS:
        raise AdapterError("Unsupported backend")
    executable = executable or os.environ.get(f"AGENT_CHAT_{name.upper()}_BIN") or name
    path = shutil.which(executable)
    if not path:
        raise AdapterError(f"{name} executable missing; add it to PATH or supply its native executable")
    path = str(Path(path).resolve())
    if Path(path).suffix.lower() in ({".bat", ".cmd", ".ps1"} if os.name == "nt" else {".exe"}):
        raise AdapterError("Use a native executable for this Python runtime")
    if model is not None and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,99}", model) or contains_secret(model)):
        raise AdapterError("Invalid model identifier")
    version = probe(path, "--version")
    help_result = probe(path, *("exec", "--help") if name == "codex" else ("--help",))
    help_text = help_result.stdout + help_result.stderr
    if name == "codex":
        root = probe(path, "--help")
        help_text += root.stdout + root.stderr
    vendor_pattern = {"codex": r"codex-cli\s+", "claude": r"\(Claude Code\)", "grok": r"^grok\s+"}[name]
    version_text = version.stdout + version.stderr
    if (version.returncode or help_result.returncode or not re.search(vendor_pattern, version_text, re.M)
            or (name == "grok" and "Grok Build" not in help_text)):
        raise AdapterError(f"Unsupported {name} CLI variant; use the official CLI")
    features = frozenset(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", help_text))
    missing = CAPABILITIES[name] - features
    if missing:
        raise AdapterError(f"{name} CLI is missing required options: {', '.join(sorted(missing))}")
    match = re.search(r"\b\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?", version_text)
    if not match:
        raise AdapterError(f"{name} CLI did not report a recognizable version")
    return Backend(name, path, match.group(), model, features)


def auth_status(backend):
    if backend.name == "codex":
        return "signed in" if probe(backend.executable, "login", "status").returncode == 0 else "sign-in unavailable"
    if backend.name == "claude":
        return "signed in" if probe(backend.executable, "auth", "status").returncode == 0 else "sign-in unavailable"
    home = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))
    if os.environ.get("XAI_API_KEY") or (home / "auth.json").is_file():
        return "credential source detected; validity unverified"
    return "no default credential source detected; check Grok login or configured provider"


def command(backend, directory, prompt):
    name = backend.name
    args = [backend.executable]
    stdin = prompt
    if name == "codex":
        schema_path = directory / "response-schema.json"
        schema_path.write_text(canonical(SCHEMA), encoding="utf-8")
        args += ["--ask-for-approval", "never", "exec", "--sandbox", "read-only",
                 "--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "--json",
                 "--output-schema", str(schema_path), "-c", 'web_search="disabled"', "-c", "mcp_servers={}"]
        for feature in ("shell_tool", "hooks", "apps", "plugins", "view_image", "browser_use",
                        "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
                        "computer_use", "multi_agent", "multi_agent_v2"):
            args += ["--disable", feature]
    elif name == "grok":
        prompt_path = directory / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        stdin = ""
        if "--no-auto-update" in backend.flags:
            args += ["--no-auto-update"]
        args += ["--prompt-file", str(prompt_path), "--output-format", "json", "--verbatim",
                 "--sandbox", "read-only", "--permission-mode", "dontAsk", "--tools", "",
                 "--deny", "MCPTool", "--no-plan", "--no-subagents", "--disable-web-search"]
        for tool in ("Bash", "Edit", "Write", "Read", "Grep", "WebSearch", "WebFetch"):
            args += ["--deny", tool]
        if "--no-memory" in backend.flags:
            args += ["--no-memory"]
        if "--json-schema" in backend.flags:
            args += ["--json-schema", canonical(SCHEMA)]
    elif name == "claude":
        args += ["--print", "--output-format", "json", "--json-schema", canonical(SCHEMA),
                 "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                 "--settings", '{"disableAllHooks":true}', "--no-session-persistence",
                 "--permission-mode", "dontAsk"]
    else:
        raise AdapterError("Unsupported backend")
    if backend.model:
        args += ["--model", backend.model]
    if name == "codex":
        args.append("-")
    return args, stdin


def error_category(output, fallback):
    text = output.casefold()
    for patterns, category in (
        (("not logged in", "not signed in", "not authenticated", "authentication failed", "unauthorized", "invalid api key",
          "login required", "please log in", "please sign in", "status 401", "status code 401"), "Backend authentication failed"),
        (("usage limit", "rate limit", "quota exceeded", "too many requests", "out of credits"), "Backend rate, usage or credit limit"),
        (("sandbox", "permission denied", "policy requires"), "Backend sandbox or permission error"),
        (("failed to connect", "connection refused", "tls", "dns"), "Backend connectivity error"),
    ):
        if any(pattern in text for pattern in patterns):
            return category
    return fallback


def safe_id(value):
    return (isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,99}", value)
            and not contains_secret(value))


def envelope_metadata(data, metadata, session_field):
    session = data.get(session_field)
    if safe_id(session):
        metadata["backend_session"] = session
    usage = data.get("usage")
    if isinstance(usage, dict):
        allowed = {"input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens",
                   "cache_creation_input_tokens", "total_tokens", "reasoning_tokens"}
        metadata["usage"] = {key: val for key, val in usage.items() if key in allowed and number(val) and val >= 0}
    cost = data.get("total_cost_usd")
    if number(cost) and cost >= 0:
        metadata["reported_cost_usd"] = cost
    models = data.get("modelUsage")
    if isinstance(models, dict):
        model_ids = [model for model in models if safe_id(model)]
        if model_ids:
            metadata["models"] = model_ids[:10]
    if safe_id(data.get("model")):
        metadata["model"] = data["model"]


def parse_output(backend, text, metadata):
    try:
        if backend == "codex":
            result = None
            completed = False
            for line in text.splitlines():
                if not line.strip():
                    continue
                event = parse_json(line)
                kind = event.get("type")
                if kind == "turn.failed":
                    raise AdapterError(error_category(canonical(event), "Codex rejected the turn"), metadata=metadata)
                if kind == "turn.started":
                    result, completed = None, False
                if kind == "thread.started":
                    envelope_metadata(event, metadata, "thread_id")
                if kind == "item.completed" and event["item"].get("type") == "agent_message":
                    result = event["item"].get("text")
                if kind == "turn.completed":
                    envelope_metadata(event, metadata, "thread_id")
                    completed = True
            if not completed:
                raise ValueError("Missing completed event")
        elif backend == "grok":
            data = parse_json(text)
            envelope_metadata(data, metadata, "sessionId")
            if data.get("error") or data.get("stopReason") not in (None, "end_turn"):
                raise AdapterError(error_category(text, "Grok did not finish successfully"), metadata=metadata)
            result = data if "message" in data and "intent" in data else data.get("structured_output", data.get("text"))
        elif backend == "claude":
            data = parse_json(text)
            if isinstance(data, list):
                data = next(event for event in reversed(data) if event.get("type") == "result")
            envelope_metadata(data, metadata, "session_id")
            if data.get("type") != "result" or data.get("subtype") != "success" or data.get("is_error"):
                raise AdapterError(error_category(text, "Claude did not finish successfully"), metadata=metadata)
            result = data.get("structured_output") or data.get("result")
        else:
            raise AdapterError("Unsupported backend")
        if isinstance(result, dict):
            result = canonical(result)
        if not isinstance(result, str) or not result.strip():
            raise ValueError("Empty response")
        return result
    except (ValueError, KeyError, AttributeError, TypeError, StopIteration, RecursionError, UnicodeError):
        raise AdapterError("Invalid or empty backend output", retry=True, metadata=metadata) from None


def terminate(process):
    if os.name == "nt":
        try:
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            subprocess.run([str(system_root / "System32" / "taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=2, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=2)


def invoke_agent(backend, prompt, timeout, cancelled, poll):
    started = time.monotonic()
    metadata = {"cli_version": backend.version}
    if backend.model:
        metadata["requested_model"] = backend.model
    if timeout <= 0 or cancelled():
        raise AdapterError("Invocation cancelled", metadata=metadata)
    with tempfile.TemporaryDirectory(prefix="agent-chat-call-") as scratch:
        directory = Path(scratch)
        args, stdin = command(backend, directory, prompt)
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            source.write(stdin.encode("utf-8"))
            source.seek(0)
            if time.monotonic() - started >= timeout:
                raise AdapterError("Model invocation timed out", metadata=metadata)
            try:
                process = subprocess.Popen(
                    args, stdin=source, stdout=stdout, stderr=stderr, cwd=directory, env=child_env(backend.name),
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
            except OSError:
                raise AdapterError("Could not start backend executable", metadata=metadata) from None
            failure = None
            try:
                while process.poll() is None:
                    if time.monotonic() - started >= timeout:
                        failure = "Model invocation timed out"
                    elif max(os.fstat(stdout.fileno()).st_size, os.fstat(stderr.fileno()).st_size) > OUTPUT_LIMIT:
                        failure = "Backend output exceeded the capture limit"
                    elif cancelled():
                        failure = "Invocation cancelled"
                    if failure:
                        break
                    try:
                        process.wait(timeout=max(0.001, min(poll, timeout - (time.monotonic() - started))))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    terminate(process)
            metadata.update(
                duration_seconds=round(time.monotonic() - started, 3), exit_code=process.returncode,
                stderr_bytes=os.fstat(stderr.fileno()).st_size,
            )
            if failure:
                raise AdapterError(failure, metadata=metadata)
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read(OUTPUT_LIMIT + 1)
            if len(output) > OUTPUT_LIMIT or metadata["stderr_bytes"] > OUTPUT_LIMIT:
                raise AdapterError("Backend output exceeded the capture limit", metadata=metadata)
            stderr_text = stderr.read(OUTPUT_LIMIT).decode("utf-8", errors="replace")
            output_text = output.decode("utf-8", errors="replace")
            if process.returncode != 0:
                try:
                    parse_output(backend.name, output_text, metadata)
                except AdapterError:
                    pass
                reason = error_category(stderr_text + "\n" + output_text, f"Backend exited with code {process.returncode}")
                raise AdapterError(reason, metadata=metadata)
            return parse_output(backend.name, output_text, metadata), metadata
