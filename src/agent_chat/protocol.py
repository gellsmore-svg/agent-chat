import errno
import hashlib
import html
import json
import math
import os
import re
import socket
import stat
import sys
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

PROTOCOL_VERSION = 2
FINISH_SEQUENCES = {
    1: ("propose_finish", "accept_finish"),
    2: ("propose_finish", "accept_finish", "confirm_finish", "confirm_finish"),
}
SOURCE_SELECTION_BYTES = 24_000
SOURCE_TOTAL_BYTES = 48_000
SOURCE_SELECTION_COUNT = 30
SOURCE_SCAN_BYTES = 1_048_576
BACKENDS = {"codex", "grok", "claude"}
INTENTS = {"continue", *FINISH_SEQUENCES[PROTOCOL_VERSION]}
TERMINAL = {
    "completed": "Completed by mutual agreement",
    "timed_out": "Timed out",
    "max_turns": "Maximum turns reached",
    "cancelled": "Cancelled",
    "failed": "Failed",
}
LABELS = {"waiting": "Waiting for both participants", "running": "In progress", **TERMINAL}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}\Z")
SECRET = re.compile(
    r"(?i)(?:\b(?:sk-(?:ant-|proj-)?|xai-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{16,}"
    r"|\bBearer\s+[A-Za-z0-9._~-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:api[_ -]?key|password|access[_ -]?token|client[_ -]?secret)\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{16,})"
)
LIMITS = {
    "duration": (0.1, 3600), "max_turns": (1, 100), "poll": (0.05, 10),
    "timeout": (0.1, 3600), "join_timeout": (0.1, 3600), "worker_grace": (0.2, 120),
    "context_chars": (8000, 100000), "message_chars": (100, 4000),
}


class ProtocolError(Exception):
    pass


class Busy(ProtocolError):
    pass


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def number(value):
    return type(value) is int or type(value) is float and math.isfinite(value)


def is_uuid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def contains_secret(value):
    if SECRET.search(value):
        return True
    return any(
        len(secret) >= 12 and secret in value
        for name, secret in os.environ.items()
        if re.search(r"API_KEY|PASSWORD|(?:^|_)TOKEN$|CLIENT_SECRET", name, re.I)
    )


def check_text(value, label):
    require(not contains_secret(value), f"{label} contains a suspected credential")
    require(not any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\r\n\t" for char in value
    ), f"{label} contains unsafe control characters")


def checked(path, kind=None):
    path = Path(path).expanduser()
    require(".." not in path.parts, "Parent traversal is not allowed in paths")
    path = Path(os.path.abspath(path))
    require(not (os.name == "nt" and str(path).startswith("\\\\")), "Use a local drive")
    if os.name == "nt":
        require(not any(":" in part or part.endswith((".", " ")) for part in path.parts[1:]) and not path.is_reserved(),
                "Windows paths must use ordinary files and directories")
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if kind and component == path:
                raise
            continue
        require(
            not (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400),
            f"Links and reparse points are not allowed here: {component}",
        )
        if component != path or kind == "dir":
            require(stat.S_ISDIR(info.st_mode), f"Expected a directory: {component}")
        elif kind == "file":
            require(stat.S_ISREG(info.st_mode), f"Expected a regular file: {component}")
    return path


def is_checkout(path):
    return ((path / "pyproject.toml").is_file()
            and (path / "src" / "agent_chat" / "protocol.py").is_file())


def session_root(path):
    root = checked(path)
    require(not any(is_checkout(parent) for parent in (root, *root.parents)),
            "Choose a session directory outside the Agent Chat source checkout")
    require(not root.is_relative_to(Path(__file__).resolve().parent),
            "Choose a session directory outside the installed Agent Chat package")
    if os.name == "nt":
        from ctypes import windll
        require(windll.kernel32.GetDriveTypeW(root.anchor) != 4, "Use a local drive")
    elif sys.platform.startswith("linux"):
        require(linux_local_mount(root), "Use a local filesystem")
    return root


def linux_local_mount(root):
    try:
        mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return True
    longest = 0
    local = True
    remote = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "ceph", "glusterfs", "fuse.sshfs", "fuse.rclone"}
    for line in mounts.splitlines():
        before, _, after = line.partition(" - ")
        mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), before.split()[4]))
        if root.is_relative_to(mount) and len(mount.parts) >= longest:
            longest = len(mount.parts)
            filesystem, source, options = after.split(maxsplit=2)
            local = filesystem not in remote and (
                filesystem != "9p" or options.startswith("rw,aname=drvfs;") or options.startswith("ro,aname=drvfs;")
            )
    return local


def text_file(path, limit):
    path = checked(path, "file")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    require(len(data) <= limit, f"File exceeds its size limit of {limit:,} bytes: {path.name}")
    return decode_source(data, path)


def decode_source(data, path, starts_at_first_line=True):
    try:
        return data.decode("utf-8-sig" if starts_at_first_line else "utf-8")
    except UnicodeError:
        raise ProtocolError(f"File must contain UTF-8 text: {path.name}") from None


def source_range_key(path, start, end):
    return f"{path}#L{start}-L{end}"


def source_range(path, start, end):
    path = checked(path, "file")
    data = bytearray()
    with path.open("rb") as source:
        remaining_scan = SOURCE_SCAN_BYTES
        for _ in range(1, start):
            line = source.readline(remaining_scan + 1)
            require(line, f"Line range starts past end of file: {path.name}")
            remaining_scan -= len(line)
            require(remaining_scan >= 0, f"Line range exceeds the {SOURCE_SCAN_BYTES:,}-byte scan limit: {path.name}")
        for line_number in range(start, end + 1):
            remaining_bytes = SOURCE_SELECTION_BYTES - len(data)
            line = source.readline(remaining_bytes + 1)
            if not line:
                position = "starts" if line_number == start else "extends"
                raise ProtocolError(f"Line range {position} past end of file: {path.name}")
            require(len(line) <= remaining_bytes,
                    f"Line range exceeds the {SOURCE_SELECTION_BYTES:,}-byte selection limit: {path.name}")
            data.extend(line)
    return decode_source(data, path, starts_at_first_line=start == 1)


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def seal(data):
    return {**data, "checksum": digest(data)}


def verify(data, label):
    try:
        valid = data["checksum"] == digest({key: val for key, val in data.items() if key != "checksum"})
    except (KeyError, ValueError, UnicodeError):
        valid = False
    require(valid, f"Invalid checksum: {label}")


def parse_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def invalid_constant(_value):
        raise ValueError("Invalid JSON number")

    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)


def read_json(path):
    try:
        data = parse_json(text_file(path, 1_000_000))
    except (ValueError, RecursionError):
        raise ProtocolError(f"Invalid JSON: {path.name}") from None
    require(isinstance(data, dict), f"Expected a JSON object: {path.name}")
    return data


def sync_dir(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
        finally:
            os.close(descriptor)


def atomic_write(path, contents):
    path = checked(path)
    if path.exists():
        checked(path, "file")
    descriptor, temp_name = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(contents.encode("utf-8"))
            target.flush()
            os.fsync(target.fileno())
        for attempt in range(4):
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 3:
                    raise
                time.sleep(0.025 * (attempt + 1))
        sync_dir(path.parent)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def write_json(path, data):
    atomic_write(path, canonical(data) + "\n")


class FileLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self, timeout=0):
        require(self.handle is None, "Lock already acquired by this handle")
        path = checked(self.path)
        if path.exists():
            checked(path, "file")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    if os.name == "nt":
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.handle = descriptor
                    return True
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    time.sleep(min(0.025, remaining))
        finally:
            if self.handle is None:
                os.close(descriptor)

    def release(self):
        if self.handle is not None:
            descriptor, self.handle = self.handle, None
            try:
                if os.name == "nt":
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def owner(instance):
    return {"instance": instance, "pid": os.getpid(), "host": socket.gethostname(), "at": time.time()}


@lru_cache(maxsize=1)
def machine_scope():
    identity = socket.gethostname()
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                identity = winreg.QueryValueEx(key, "MachineGuid")[0]
        elif sys.platform.startswith("linux"):
            identity = Path("/etc/machine-id").read_text(encoding="ascii").strip() or identity
    except (OSError, UnicodeError):
        pass
    return digest([sys.platform, identity])


def validate_config(config):
    verify(config, "session.json")
    version = config.get("version")
    require(type(version) is int and version in FINISH_SEQUENCES,
            "Incompatible session protocol. This installation supports protocol v1 and v2.")
    require(config.get("mode") == "discussion", "Unsupported operating mode")
    if "machine_scope" in config:
        require(config["machine_scope"] == machine_scope(),
                "Session belongs to another machine or OS runtime")
    require(is_uuid(config.get("id")) and number(config.get("created")), "Invalid session identity")
    participants = config.get("participants")
    require(isinstance(participants, list) and len(participants) == 2, "Exactly two participants are required")
    names = []
    for participant in participants:
        require(isinstance(participant, dict), "Invalid participant")
        name = participant.get("name")
        require(isinstance(name, str) and NAME.fullmatch(name) and name == name.strip(), "Invalid participant name")
        require(not contains_secret(name), "Participant name resembles a credential")
        backend = participant.get("backend")
        require(isinstance(backend, str) and backend in BACKENDS,
                "Unsupported backend (expected codex, grok, or claude)")
        names.append(name)
    require(names[0].casefold() != names[1].casefold() and config.get("first") in names, "Invalid speaker identities")
    for field, (minimum, maximum) in LIMITS.items():
        value = config.get(field)
        require(number(value) and minimum <= value <= maximum, f"{field} must be between {minimum} and {maximum}")
        if field in {"max_turns", "context_chars", "message_chars"}:
            require(type(value) is int, f"{field} must be an integer")
    require(config["worker_grace"] >= 2 * config["poll"], "Worker grace must be at least twice the poll interval")
    for field, limit in (("objective", 6000), ("title", 120)):
        value = config.get(field)
        require(isinstance(value, str) and 0 < len(value.strip()) <= limit, f"Invalid {field}")
        check_text(value, field.capitalize())
    sources = config.get("sources")
    require(isinstance(sources, dict) and all(isinstance(value, str) for value in sources.values()), "Invalid project snapshot")
    for name, value in sources.items():
        check_text(name, "Project filename")
        check_text(value, "Project snapshot")
    minimum_context = len(canonical(sources)) + len(canonical(config["objective"])) + 4 * config["message_chars"] + 4500
    if "source_provenance" in config:
        provenance = config["source_provenance"]
        require(isinstance(provenance, dict), "Invalid source provenance")
        for name, entry in provenance.items():
            require(name in sources and isinstance(entry, dict) and set(entry) == {"path", "start", "end", "bytes"},
                    "Invalid source provenance")
            path, start, end, size = (entry[field] for field in ("path", "start", "end", "bytes"))
            require(isinstance(path, str) and path and (path not in sources or path in provenance)
                    and type(start) is int and type(end) is int and 1 <= start <= end
                    and name == source_range_key(path, start, end)
                    and type(size) is int and size == len(sources[name].encode("utf-8")), "Invalid source provenance")
        minimum_context += len(canonical(provenance))
    require(config["context_chars"] >= minimum_context, f"Context budget must be at least {minimum_context} characters for these inputs")


def validate_control(config, control):
    status = control.get("status")
    require(control.get("session") == config["id"] and isinstance(status, str) and status in LABELS,
            "Invalid lifecycle state")
    require(isinstance(control.get("reason"), str) and isinstance(control.get("error"), bool), "Invalid lifecycle result")
    registrations = control.get("registrations")
    require(isinstance(registrations, dict) and set(registrations) <= {"0", "1"}, "Invalid registration table")
    for slot, record in registrations.items():
        participant = config["participants"][int(slot)]
        require(isinstance(record, dict) and record.get("name") == participant["name"]
                and record.get("backend") == participant["backend"] and is_uuid(record.get("instance"))
                and number(record.get("at")), "Invalid worker registration")
        if "machine_scope" not in config:
            require(record.get("host") == socket.gethostname(),
                    "Legacy session registration belongs to another host")
    started, deadline, joined = (control.get(key) for key in ("started", "deadline", "join_deadline"))
    require(
        (started is None and deadline is None) or (
            number(started) and number(deadline) and deadline == started + config["duration"]
            and len(registrations) == 2
        ), "Invalid active clock",
    )
    require((not registrations and joined is None) or (registrations and number(joined)), "Invalid join clock")
    require(
        (control["status"] != "waiting" or started is None)
        and (control["status"] != "running" or started is not None)
        and control["error"] == (control["status"] == "failed"), "Inconsistent lifecycle state",
    )
    inflight = control.get("inflight")
    if inflight is not None:
        require(
            control["status"] == "running" and isinstance(inflight, dict)
            and is_uuid(inflight.get("id")) and type(inflight.get("slot")) is int
            and str(inflight["slot"]) in registrations
            and inflight.get("instance") == registrations[str(inflight["slot"])]["instance"]
            and type(inflight.get("sequence")) is int and inflight["sequence"] > 0
            and number(inflight.get("at"))
            and type(inflight.get("attempt")) is int and inflight["attempt"] in (1, 2),
            "Invalid invocation claim",
        )


def speaker(config, sequence):
    first_slot = int(config["participants"][1]["name"] == config["first"])
    return (first_slot + sequence - 1) % 2


def finish_progress(version, turns):
    sequence = FINISH_SEQUENCES[version]
    recent = tuple(turn["intent"] for turn in turns[-len(sequence):])
    for length in range(len(recent), 0, -1):
        if recent[-length:] == sequence[:length]:
            return length
    return 0


def allowed_intents(version, turns):
    sequence = FINISH_SEQUENCES[version]
    progress = finish_progress(version, turns)
    if progress == len(sequence):
        return set()
    return {"continue", "propose_finish", sequence[progress]}


def finish_notice(config, turns):
    if config["version"] == 1:
        return ""
    progress = finish_progress(config["version"], turns)
    if not 1 <= progress <= 3:
        return ""
    proposal = len(turns) - progress + 1
    name = config["participants"][speaker(config, len(turns) + 1)]["name"]
    if progress == 1:
        return f"Proposal in turn {proposal} awaits acceptance from {name}."
    return f"Proposal in turn {proposal} accepted. {progress - 2} of 2 confirmations recorded. Awaiting {name}."


def outcome(control, status, reason):
    control.update(status=status, reason=reason, error=status == "failed", inflight=None)


def quoted(value):
    value = html.escape(value, quote=False).replace("![", "&#33;[")
    return "\n".join(f"> {line}" if line else ">" for line in value.splitlines())


def render(config, control, turns):
    names = ", ".join(f"{item['name']} ({item['backend']})" for item in config["participants"])
    title = html.escape(" ".join(config["title"].split()), quote=False)
    sections = [
        f"# Agent Chat · {title}\n\n**Participants:** {names}\n\n"
        f"**Objective**\n\n{quoted(config['objective'])}\n\n"
        f"**Status:** {LABELS[control['status']]}\n\n"
        f"**Limits:** {config['duration']:g}s active, {config['max_turns']} turns, "
        f"{config['timeout']:g}s per invocation · discussion mode",
    ]
    if control["status"] == "running" and (notice := finish_notice(config, turns)):
        sections[0] += f"\n\n**Finish check:** {notice}"
    labels = {"decisions": "Decisions", "open_questions": "Challenges and open questions", "actions": "Action items"}
    for turn in turns:
        entry = f"## Turn {turn['sequence']} · {turn['author']}\n\n**Time:** {timestamp(turn['at'])}"
        if config["version"] != 1:
            entry += f"\n\n**Intent:** `{turn['intent']}`"
        entry += "\n\n" + quoted(turn["message"])
        for field, label in labels.items():
            if turn[field]:
                entry += f"\n\n**{label}**\n\n" + quoted("\n".join(f"- {item}" for item in turn[field]))
        sections.append(entry)
    if control["status"] in TERMINAL:
        sections.append(f"## Session outcome\n\n**{LABELS[control['status']]}**\n\n{control['reason']}")
        if control["status"] == "completed" and config["version"] != 1:
            sections[-1] += (f" See proposal in turn {len(turns) - 3}, acceptance in turn {len(turns) - 2},"
                             f" and confirmations in turns {len(turns) - 1} and {len(turns)}.")
        elif control["status"] == "completed":
            sections[-1] += f" See proposal in turn {len(turns) - 1} and acceptance in turn {len(turns)}."
        elif turns:
            sections[-1] += f" See turn {len(turns)} for the latest position and unresolved matters."
    return "\n\n---\n\n".join(sections) + "\n"


class Store:
    def __init__(self, directory):
        self.root = checked(session_root(directory), "dir")
        self.instance = uuid.uuid4().hex
        self.config = None

    def path(self, *parts):
        return self.root.joinpath(*parts)

    @contextmanager
    def transaction(self, timeout=3):
        lock = FileLock(self.path("write.lock"))
        if not lock.acquire(timeout=timeout):
            raise Busy("Commit lock is busy; retry after the active transaction")
        try:
            write_json(self.path("lock-owner.json"), owner(self.instance))
            yield
        finally:
            lock.release()

    def load(self):
        if self.config is None:
            require(self.path("session.json").exists(), "No published session; initialization may be incomplete")
            config = read_json(self.path("session.json"))
            validate_config(config)
            self.config = config
        return self.config

    def initialize(self, config):
        with self.transaction():
            if self.path("session.json").exists():
                self.state()
                return False
            existing = {entry.name for entry in self.root.iterdir()} - {"write.lock", "lock-owner.json"}
            require(not existing, "Directory is not empty or contains incomplete initialization")
            validate_config(config)
            for name in ("turns", "leases", "diagnostics"):
                self.path(name).mkdir(mode=0o700)
            control = {
                "session": config["id"], "status": "waiting", "registrations": {}, "started": None,
                "deadline": None, "join_deadline": None, "reason": "", "error": False, "inflight": None,
            }
            self.publish(config, control, [])
            write_json(self.path("session.json"), config)
            return True

    def state(self):
        config = self.load()
        control = read_json(self.path("control.json"))
        verify(control, "control.json")
        del control["checksum"]
        validate_control(config, control)
        turns = self.turns(config, control)
        return config, control, turns

    def turns(self, config, control):
        folder = checked(self.path("turns"), "dir")
        entries = sorted(entry for entry in folder.iterdir() if not re.fullmatch(r"\.partial-[a-z0-9_]+", entry.name))
        require(len(entries) <= config["max_turns"], "Journal exceeds turn limit")
        turns = []
        invocations = set()
        for sequence, entry in enumerate(entries, 1):
            require(entry.name == f"{sequence:06}.json", f"Unexpected file or gap in journal: {entry.name}")
            turn = read_json(entry)
            verify(turn, entry.name)
            participant = config["participants"][speaker(config, sequence)]
            previous = turns[-1]["checksum"] if turns else None
            require(
                turn.get("session") == config["id"] and type(turn.get("sequence")) is int
                and turn["sequence"] == sequence and turn.get("author") == participant["name"]
                and turn.get("backend") == participant["backend"] and turn.get("previous") == previous
                and is_uuid(turn.get("invocation")) and turn["invocation"] not in invocations
                and number(turn.get("at")) and control["started"] is not None
                and control["started"] <= turn["at"] < control["deadline"]
                and (not turns or turn["at"] >= turns[-1]["at"]),
                f"Inconsistent journal record: {entry.name}",
            )
            validate_contribution(turn, config["message_chars"], config["version"], turns)
            turns.append(turn)
            invocations.add(turn["invocation"])
        return turns

    def publish(self, config, control, turns):
        self.save_control(control)
        self.transcript(config, control, turns)

    def save_control(self, control):
        write_json(self.path("control.json"), seal(control))

    def transcript(self, config, control, turns):
        atomic_write(self.path("conversation.md"), render(config, control, turns))

    def worker_lock(self, slot):
        checked(self.path("leases"), "dir")
        return FileLock(self.path("leases", f"{slot}.lock"))

    def lease(self, slot):
        return read_json(self.path("leases", f"{slot}.json"))

    def heartbeat(self, slot):
        write_json(self.path("leases", f"{slot}.json"), owner(self.instance))

    def live(self, slot, registration, now, grace, require_lease=True):
        probe = self.worker_lock(slot)
        if probe.acquire():
            probe.release()
            return False
        if not require_lease:
            return True
        try:
            lease = self.lease(slot)
        except FileNotFoundError:
            return False
        return (lease.get("instance") == registration["instance"] and number(lease.get("at"))
                and now - lease["at"] <= grace)


def validate_contribution(data, limit, version, turns):
    message, intent = data.get("message"), data.get("intent")
    require(isinstance(message, str), f"Contribution message must be text (limit {limit:,} characters)")
    length = len(message.strip())
    require(length > 0, f"Contribution message is empty (limit {limit:,} characters)")
    require(length <= limit, f"Contribution message is {length:,} characters after trimming (limit {limit:,})")
    require(isinstance(intent, str) and intent in INTENTS, "Invalid contribution intent")
    reasons = {"accept_finish": "Acceptance requires the preceding finish proposal",
               "confirm_finish": "Confirmation requires the current accepted finish proposal"}
    require(intent in allowed_intents(version, turns),
            reasons.get(intent, "Contribution after mutual completion"))
    for field in ("decisions", "open_questions", "actions"):
        entries = data.get(field)
        require(
            isinstance(entries, list) and len(entries) <= 4 and all(
                isinstance(entry, str) and 0 < len(entry.strip()) <= 180 and len(entry.splitlines()) == 1
                for entry in entries
            ), f"Invalid contribution field: {field}",
        )
    if version != 1 and intent != "continue":
        require(not (data["open_questions"] or data["actions"]),
                "Finish requires empty open_questions and actions. Use continue if work remains.")
    check_text("\n".join([message, *(item for field in ("decisions", "open_questions", "actions")
                                     for item in data[field])]), "Response")
