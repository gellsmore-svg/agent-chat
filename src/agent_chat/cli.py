import argparse
import os
import re
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import __version__
from .adapters import AdapterError, auth_status, discover, invoke_agent
from .installer import DISCOVERY, SKILL, home_skills, install
from .protocol import (
    BACKENDS, LABELS, PROTOCOL_VERSION, SOURCE_SELECTION_BYTES, SOURCE_SELECTION_COUNT, SOURCE_TOTAL_BYTES,
    TERMINAL, FileLock, ProtocolError, Store, atomic_write, checked, finish_notice, machine_scope, outcome, quoted, render,
    require, seal, session_root, source_range, source_range_key, speaker, text_file, validate_config,
)
from .runner import build_prompt, normalize, refresh, run_worker


def participant(value):
    name, separator, backend = value.rpartition(":")
    if not separator or not name or backend not in BACKENDS:
        raise argparse.ArgumentTypeError("expected NAME:codex, NAME:grok, or NAME:claude")
    return {"name": name, "backend": backend}


def range_selector(value):
    path, _, bounds = value.rpartition("=")
    match = re.fullmatch(r"([0-9]+)-([0-9]+)", bounds)
    if path and match:
        try:
            start, end = map(int, match.groups())
        except ValueError:
            pass
        else:
            if 1 <= start <= end:
                return path, start, end
    raise argparse.ArgumentTypeError("expected PATH=START-END with positive, ascending line numbers")


def source_path(file_name):
    reference = Path(file_name)
    require(not (reference.is_absolute() or reference.anchor or ".." in reference.parts),
            "Source selections must use a relative path within the project")
    require(all(not part.startswith(".") for part in reference.parts), "Hidden project paths are excluded")
    return reference


def selected_sources(directory, files, ranges):
    if directory is None:
        require(not (files or ranges), "--include and --range require --project")
        return {}, {}
    root = checked(directory, "dir")
    require(files or ranges, "--project requires at least one --include or --range selection")
    selected = {}
    provenance = {}
    size = 0

    def add_selection(key, content):
        nonlocal size
        selected[key] = content
        content_bytes = len(content.encode("utf-8"))
        size += content_bytes
        require(size <= SOURCE_TOTAL_BYTES and len(selected) <= SOURCE_SELECTION_COUNT,
                f"Project snapshot exceeds {SOURCE_TOTAL_BYTES:,} bytes or {SOURCE_SELECTION_COUNT} selections")
        return content_bytes

    whole_files = {source_path(name): None for name in files}
    for reference in whole_files:
        add_selection(reference.as_posix(), text_file(root / reference, SOURCE_SELECTION_BYTES))
    for file_name, start, end in ranges:
        reference = source_path(file_name)
        path = reference.as_posix()
        require(reference not in whole_files, f"Choose either --include or --range for {path}")
        key = source_range_key(path, start, end)
        if key in provenance:
            continue
        require(key not in selected, f"Source range label conflicts with a selected file: {key}")
        content_bytes = add_selection(key, source_range(root / reference, start, end))
        provenance[key] = {"path": path, "start": start, "end": end, "bytes": content_bytes}
    return selected, provenance


def initialize(args):
    root = session_root(args.dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(root)
    if store.path("session.json").exists():
        with store.transaction():
            store.state()
        print(f"Existing session recognized at {root}")
        return 0
    objective = args.objective if args.objective is not None else text_file(args.objective_file, 24_000)
    sources, provenance = selected_sources(args.project, args.include, args.range)
    config = {
        "version": PROTOCOL_VERSION, "machine_scope": machine_scope(),
        "id": uuid.uuid4().hex, "created": time.time(),
        "mode": "discussion", "participants": [args.a, args.b], "first": args.first or args.a["name"],
        "objective": objective.strip(), "title": args.title or root.name,
        "duration": args.minutes * 60, "max_turns": args.max_turns, "poll": args.poll,
        "timeout": args.timeout, "join_timeout": args.join_timeout, "worker_grace": args.worker_grace,
        "context_chars": args.context_chars, "message_chars": args.message_chars,
        "sources": sources,
    }
    if provenance:
        config["source_provenance"] = provenance
    config = seal(config)
    validate_config(config)
    build_prompt(config, 0, [])
    created = store.initialize(config)
    print(f"{'Initialized' if created else 'Existing session recognized at'} {root}")
    return 0


def run(args):
    store = Store(args.dir)
    config = store.load()
    slot = next((index for index, item in enumerate(config["participants"]) if item["name"] == args.agent), None)
    require(slot is not None, "Unknown participant; use exactly one configured identity")
    with store.transaction():
        _, control, turns = store.state()
        refresh(store, config, control, turns)
        if control["status"] in TERMINAL:
            print(f"{LABELS[control['status']]} · {control['reason']}")
            return int(control["status"] == "failed")
    backend = discover(config["participants"][slot]["backend"], args.executable, args.model)
    return run_worker(store, slot, backend)


def summarize(turns):
    sections = ["## Speaker-attributed notes"]
    for field, label in (("decisions", "Decisions"), ("actions", "Action items"),
                         ("open_questions", "Recorded questions, including earlier unresolved items")):
        entries = [f"- Turn {turn['sequence']} · {turn['author']}: {item}"
                   for turn in turns for item in turn[field]]
        if entries:
            sections.append(f"### {label}\n\n" + quoted("\n".join(entries)))
    return "\n\n".join(sections) + "\n"


def manage(args):
    store = Store(args.dir)
    with store.transaction():
        config, control, turns = store.state()
        refresh(store, config, control, turns, persist=args.command != "export")
        if args.command == "stop" and control["status"] not in TERMINAL:
            outcome(control, "cancelled", "Cancelled by the user.")
            store.publish(config, control, turns)
        if args.command == "recover":
            partials = sum(1 for directory in (store.root, *(store.path(name) for name in ("turns", "leases", "diagnostics")))
                           for entry in checked(directory, "dir").glob(".partial-*"))
            store.transcript(config, control, turns)
            print(f"Validated {len(turns)} committed turns and rebuilt conversation.md")
            if partials:
                print(f"Preserved {partials} interrupted temporary file(s)")
            if control["inflight"]:
                print("An invocation claim is present. A live worker can finish it, otherwise worker grace applies.")
        if args.command == "export":
            output = render(config, control, turns)
            if args.summary:
                output += "\n---\n\n" + summarize(turns)
            if args.output:
                destination = checked(args.output)
                require(not destination.is_relative_to(store.root), "Export to a path outside the session")
                require(not destination.exists(), "Export destination already exists")
                checked(destination.parent, "dir")
                atomic_write(destination, output)
            else:
                print(output, end="")
            return 0
        print(f"{LABELS[control['status']]} · {len(turns)} committed turn(s)")
        if control["reason"]:
            print(control["reason"])
        if args.command == "status":
            now = time.time()
            for slot, item in enumerate(config["participants"]):
                record = control["registrations"].get(str(slot))
                state = "not registered"
                if record:
                    state = "active" if store.live(slot, record, now, config["worker_grace"]) else "registered, inactive"
                print(f"{item['name']} ({item['backend']}): {state}")
            if control["status"] == "running":
                name = config["participants"][speaker(config, len(turns) + 1)]["name"]
                print(f"Next speaker: {name} · {max(0, control['deadline'] - now):.1f}s remaining")
                if notice := finish_notice(config, turns):
                    print(notice)
            if control["status"] == "waiting" and control["join_deadline"]:
                print(f"Join window: {max(0, control['join_deadline'] - now):.1f}s remaining")
            if turns:
                previous = turns[-1]
                print(f"Last contribution by {previous['author']}:")
                print(quoted(previous["message"]))
    return 0


def doctor(args):
    print(f"Agent Chat {__version__} · protocol v{PROTOCOL_VERSION}")
    print(f"Python {sys.version.split()[0]} · {os.name} · cwd {Path.cwd()}")
    root = checked(args.dir or Path.cwd(), "dir")
    with tempfile.TemporaryDirectory(prefix=".agent-chat-doctor-", dir=root) as scratch:
        path = Path(scratch)
        atomic_write(path / "probe", "before")
        atomic_write(path / "probe", "after")
        lock = FileLock(path / "probe.lock")
        require(lock.acquire(), "Filesystem lock check failed")
        try:
            require(not FileLock(path / "probe.lock").acquire(), "Filesystem permits duplicate locks")
        finally:
            lock.release()
    print("Filesystem write, replace and exclusive lock checks passed")
    if args.dir:
        store = Store(root)
        with store.transaction():
            config, control, turns = store.state()
            print(f"Session configuration valid · {control['status']} · {len(turns)} turn(s)")
    failed = False
    for name in args.backend or sorted(BACKENDS):
        configured = [directory / DISCOVERY[name] / "skills" for directory in (root, *root.parents)]
        configured.append(home_skills(name))
        installed = next((folder / SKILL.name for folder in configured
                          if (folder / SKILL.name / "SKILL.md").is_file()), None)
        try:
            backend = discover(name)
            print(f"{name} {backend.version} · compatible flags · {auth_status(backend)}", flush=True)
            if args.smoke:
                sample = {
                    "version": PROTOCOL_VERSION,
                    "objective": "Give one concise, concrete advantage of checking a calculation twice. Use continue.",
                    "participants": [{"name": "Local"}, {"name": "Peer"}], "sources": {},
                    "max_turns": 2, "message_chars": 1000, "context_chars": 8000,
                }
                raw, metadata = invoke_agent(backend, build_prompt(sample, 0, []), 45, lambda: False, 0.25)
                normalize(raw, sample, [])
                print(f"{name}: live structured turn validated in {metadata['duration_seconds']}s")
        except (AdapterError, ProtocolError) as error:
            print(f"{name}: {error}")
            failed = True
        print(f"{name} skill: {installed or 'discovery location missing; standalone runner available'}")
    return int(failed)


def parser():
    cli = argparse.ArgumentParser(description="Two-party local Agent Chat")
    cli.add_argument("--version", action="version", version=f"Agent Chat {__version__} (protocol v{PROTOCOL_VERSION})")
    commands = cli.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a two-agent session")
    init.add_argument("--dir", required=True)
    init.add_argument("--a", type=participant, required=True)
    init.add_argument("--b", type=participant, required=True)
    init.add_argument("--first")
    init.add_argument("--title")
    objective = init.add_mutually_exclusive_group(required=True)
    objective.add_argument("--objective")
    objective.add_argument("--objective-file")
    init.add_argument("--minutes", type=float, default=10)
    init.add_argument("--max-turns", type=int, default=12)
    init.add_argument("--poll", type=float, default=1)
    init.add_argument("--timeout", type=float, default=120)
    init.add_argument("--join-timeout", type=float, default=120)
    init.add_argument("--worker-grace", type=float, default=15)
    init.add_argument("--context-chars", type=int, default=36000)
    init.add_argument("--message-chars", type=int, default=2400)
    init.add_argument("--project")
    init.add_argument("--include", action="append", default=[])
    init.add_argument("--range", action="append", default=[], type=range_selector, metavar="PATH=START-END",
                      help="snapshot a project's exact 1-based inclusive line range")
    worker = commands.add_parser("run", help="join and run one participant")
    worker.add_argument("--dir", required=True)
    worker.add_argument("--agent", required=True)
    worker.add_argument("--model")
    worker.add_argument("--executable", help="trusted native executable for this participant")
    for action in ("status", "stop", "recover", "export"):
        command = commands.add_parser(action)
        command.add_argument("--dir", required=True)
        if action == "export":
            command.add_argument("--output")
            command.add_argument("--summary", action="store_true")
    check = commands.add_parser("doctor", help="diagnose local CLI and filesystem compatibility")
    check.add_argument("--dir")
    check.add_argument("--backend", choices=sorted(BACKENDS), action="append")
    check.add_argument("--smoke", action="store_true", help="make one live model call per selected backend")
    installation = commands.add_parser("install", help="link or copy the canonical skill into discovery locations")
    installation.add_argument("--root", help="project root or home directory (default: current user's home)")
    installation.add_argument("--backend", choices=sorted(BACKENDS), action="append")
    installation.add_argument("--copy", action="store_true")
    return cli


def main():
    args = parser().parse_args()
    if args.command == "run":
        signal.signal(signal.SIGTERM, interrupt)
    execute = {"init": initialize, "run": run, "install": install, "doctor": doctor}.get(args.command, manage)
    try:
        return execute(args)
    except (AdapterError, ProtocolError, OSError, ValueError, OverflowError) as error:
        print(f"Agent Chat: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def interrupt(_signal, _frame):
    raise KeyboardInterrupt
