import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from contextlib import redirect_stdout

from agent_chat import adapters, cli, installer, protocol, runner


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent chat protocol ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_process_death_releases_lock_without_deletion(self):
        path = self.root / "stable.lock"
        fake = Path(__file__).with_name("fake_agent.py")
        owner = subprocess.Popen([sys.executable, str(fake), "lock", str(path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(owner.stdout.readline().strip(), "held")
            self.assertFalse(protocol.FileLock(path).acquire(timeout=.1))
            inode = path.stat().st_ino
        finally:
            owner.kill()
            owner.communicate(timeout=5)
        lock = protocol.FileLock(path)
        self.assertTrue(lock.acquire(timeout=.3))
        lock.release()
        self.assertEqual(path.stat().st_ino, inode)

    def test_concurrent_commit_attempts_publish_once(self):
        session = self.root / "session"
        args = cli.parser().parse_args([
            "init", "--dir", str(session), "--objective", "Test contention", "--a", "One:codex", "--b", "Two:grok",
        ])
        cli.initialize(args)
        first, second = protocol.Store(session), protocol.Store(session)
        locks = [first.worker_lock(0), second.worker_lock(1)]
        for lock in locks:
            self.addCleanup(lock.release)
        runner.register(first, 0, locks[0])
        runner.register(second, 1, locks[1])
        _, _, claim = runner.reserve(first, 0)
        response = {"message": "Check the stated assumption.", "intent": "continue",
                    "decisions": [], "open_questions": [], "actions": []}
        gate = threading.Barrier(2)
        results = []

        def commit():
            contender = protocol.Store(session)
            contender.instance = first.instance
            gate.wait(timeout=3)
            try:
                runner.resolve(contender, claim, response=response)
                results.append("committed")
            except protocol.ProtocolError:
                results.append("rejected")

        threads = [threading.Thread(target=commit) for _ in locks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(results, ["committed", "rejected"])
        _, _, turns = first.state()
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["invocation"], claim["id"])

    def test_context_keeps_current_exchange_and_historical_notes(self):
        config = {
            "version": protocol.PROTOCOL_VERSION,
            "objective": "Resolve the architecture.", "participants": [{"name": "One"}, {"name": "Two"}],
            "max_turns": 12, "sources": {}, "context_chars": 8000, "message_chars": 1000,
        }
        history = [
            {"sequence": index, "author": "One" if index % 2 else "Two", "intent": "continue",
             "message": "detail " * 200, "decisions": [f"Recorded decision {index}"],
             "open_questions": [f"Open question {index}"], "actions": []}
            for index in range(1, 12)
        ]
        prompt = runner.build_prompt(config, 1, history)
        data = json.loads(prompt.split("\n```json\n")[1].split("\n```")[0])
        self.assertLessEqual(len(prompt), config["context_chars"])
        self.assertGreater(data["omitted_turn_count"], 0)
        self.assertEqual(data["recent_original_turns"][-1]["sequence"], 11)
        self.assertIn("Recorded decision 1", prompt)
        self.assertIn("Open question 1", prompt)

    def test_confirmation_retains_the_proposal_and_acceptance_when_compacting_history(self):
        config = {
            "version": protocol.PROTOCOL_VERSION,
            "objective": "Resolve the architecture.", "participants": [{"name": "One"}, {"name": "Two"}],
            "max_turns": 12, "sources": {}, "context_chars": 8000, "message_chars": 1000,
        }
        intents = ["continue"] * 7 + ["propose_finish", "accept_finish", "confirm_finish"]
        turns = [
            {"sequence": index, "author": "One" if index % 2 else "Two", "intent": intent,
             "message": "detail " * 140, "decisions": [], "open_questions": [], "actions": []}
            for index, intent in enumerate(intents, 1)
        ]
        prompt = runner.build_prompt(config, 0, turns)
        data = json.loads(prompt.split("\n```json\n")[1].split("\n```")[0])
        self.assertGreater(data["omitted_turn_count"], 0)
        self.assertEqual(data["finish_check"], {"proposal_turn": 8, "confirmations_recorded": 1})
        self.assertEqual([turn["sequence"] for turn in data["recent_original_turns"][-3:]], [8, 9, 10])
        self.assertIn("confirm_finish", data["allowed_intents"])
        self.assertNotIn("accept_finish", data["allowed_intents"])

        replacement = [*turns, {"intent": "propose_finish"}]
        self.assertEqual(protocol.allowed_intents(config["version"], replacement), {
            "continue", "propose_finish", "accept_finish",
        })

    def test_atomic_replacement_and_symlink_rejection(self):
        path = self.root / "transcript with spaces.md"
        protocol.atomic_write(path, "original")
        protocol.atomic_write(path, "updated")
        self.assertEqual(path.read_text(encoding="utf-8"), "updated")
        link = self.root / "redirect"
        try:
            link.symlink_to(path)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaisesRegex(protocol.ProtocolError, "Links and reparse"):
            protocol.atomic_write(link, "unexpected")
        self.assertEqual(path.read_text(encoding="utf-8"), "updated")

    def test_incomplete_initialization_and_lock_metadata(self):
        store = protocol.Store(self.root)
        protocol.write_json(store.path("lock-owner.json"), protocol.owner(uuid.uuid4().hex))
        with store.transaction():
            self.assertEqual(protocol.read_json(store.path("lock-owner.json"))["pid"], os.getpid())
            with self.assertRaisesRegex(protocol.ProtocolError, "initialization may be incomplete"):
                store.state()

    def test_export_does_not_publish_reconciled_state(self):
        session = self.root / "session"
        args = cli.parser().parse_args([
            "init", "--dir", str(session), "--objective", "Test export", "--a", "One:codex", "--b", "Two:grok",
        ])
        cli.initialize(args)
        store = protocol.Store(session)
        lock = store.worker_lock(0)
        try:
            runner.register(store, 0, lock)
        finally:
            lock.release()
        _, control, _ = store.state()
        control["join_deadline"] = time.time() - 1
        store.save_control(control)
        transcript = store.path("conversation.md").read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            cli.manage(cli.parser().parse_args(["export", "--dir", str(session)]))
        self.assertIn("Join timeout", output.getvalue())
        self.assertEqual(store.path("conversation.md").read_bytes(), transcript)
        self.assertEqual(store.state()[1]["status"], "waiting")

    def test_install_copy_is_idempotent(self):
        args = cli.parser().parse_args(["install", "--root", str(self.root), "--copy"])
        output = io.StringIO()
        with redirect_stdout(output):
            installer.install(args)
            installer.install(args)
        for backend, folder in installer.DISCOVERY.items():
            copy = self.root / folder / "skills" / "agent-chat"
            self.assertTrue((copy / "SKILL.md").is_file())
            self.assertEqual([entry.name for entry in copy.iterdir()], ["SKILL.md"])
            self.assertFalse(copy.is_symlink())
            self.assertIn(f"{backend}: already installed", output.getvalue())

    def test_installer_migrates_legacy_repository_link(self):
        checkout = self.root / "legacy-checkout"
        package = checkout / "src" / "agent_chat"
        asset = package / installer.ASSET
        asset.mkdir(parents=True)
        (asset / "SKILL.md").touch()
        (package / "protocol.py").touch()
        (checkout / "pyproject.toml").touch()
        destination = self.root / ".agents" / "skills" / "agent-chat"
        destination.parent.mkdir(parents=True)
        try:
            destination.symlink_to(checkout, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        args = cli.parser().parse_args(["install", "--root", str(self.root), "--backend", "codex", "--copy"])
        with redirect_stdout(io.StringIO()):
            installer.install(args)
        self.assertTrue(installer.copy_matches(destination))
        self.assertTrue((checkout / "pyproject.toml").is_file())


class AdapterTest(unittest.TestCase):
    def test_grok_structured_and_usage(self):
        data = {
            "text": '{"message":"Explain the result.","intent":"continue"}', "stopReason": "end_turn",
            "sessionId": "grok-run", "usage": {"input_tokens": 5, "unrelated": 100},
            "total_cost_usd": .003, "modelUsage": {"grok-model": {"inputTokens": 5}},
        }
        metadata = {}
        self.assertEqual(json.loads(adapters.parse_output("grok", json.dumps(data), metadata))["intent"], "continue")
        self.assertEqual(metadata["usage"], {"input_tokens": 5})
        self.assertEqual(metadata["reported_cost_usd"], .003)
        self.assertEqual(metadata["models"], ["grok-model"])

    def test_parse_failures(self):
        for name, output in (("codex", '{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}'),
                             ("grok", '{"text":"partial","stopReason":"max_tokens"}'),
                             ("claude", '{"type":"result","subtype":"error","result":"partial"}')):
            with self.subTest(name=name), self.assertRaises(adapters.AdapterError):
                adapters.parse_output(name, output, {})

    def test_unrelated_variant_is_rejected(self):
        with self.assertRaisesRegex(adapters.AdapterError, "Unsupported grok CLI variant"):
            adapters.discover("grok", sys.executable)


if __name__ == "__main__":
    unittest.main()
