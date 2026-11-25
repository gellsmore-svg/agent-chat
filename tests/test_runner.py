import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_chat import protocol

FAKE = Path(__file__).with_name("fake_agent.py")


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent chat test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "conversation with spaces"
        self.log = self.root / "invocations"
        self.log.mkdir()
        self.workers = []
        self.addCleanup(self.cleanup_workers)

    def cleanup_workers(self):
        for worker in self.workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=8)

    def cli(self, action, *arguments, ok=True):
        result = subprocess.run([sys.executable, "-m", "agent_chat", action, "--dir", str(self.session), *arguments],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(ok, result.returncode == 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def init(self, *extra, backend_a="codex", backend_b="grok", ok=True):
        return self.cli("init", "--a", f"Alice:{backend_a}", "--b", f"Bob:{backend_b}",
                        "--objective", "Discuss how to check a calculation.", "--minutes", ".09", "--poll", ".07",
                        "--worker-grace", "1.2", "--timeout", "2", "--join-timeout", "2", *extra, ok=ok)

    def worker(self, agent, scenario):
        worker = subprocess.Popen(
            [sys.executable, str(FAKE), "worker", "run", "--dir", str(self.session), "--agent", agent],
            env={**os.environ, "AGENT_CHAT_TEST_SCENARIO": scenario, "AGENT_CHAT_TEST_LOG": str(self.log)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )
        self.workers.append(worker)
        return worker

    def finish(self, worker, code):
        output, _ = worker.communicate(timeout=10)
        self.assertEqual(worker.returncode, code, output)
        return output

    def pair(self, scenario, code=0):
        first = self.worker("Alice", scenario)
        second = self.worker("Bob", scenario)
        self.finish(first, code)
        self.finish(second, code)

    def read(self, file):
        return json.loads((self.session / file).read_text(encoding="utf-8"))

    def control(self):
        return self.read("control.json")

    def turns(self):
        return [self.read(path.relative_to(self.session)) for path in sorted((self.session / "turns").glob("*.json"))]

    def until(self, predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        self.fail("state did not arrive before deadline")

    def test_all_backend_pairs_alternate_and_finish(self):
        for number, pair in enumerate((("codex", "grok"), ("claude", "codex"), ("grok", "claude"))):
            with self.subTest(pair=pair):
                self.session = self.root / f"conversation {number}"
                self.init("--first", "Bob", backend_a=pair[0], backend_b=pair[1])
                self.pair("mutual")
                turns = self.turns()
                self.assertEqual([turn["author"] for turn in turns], ["Bob", "Alice", "Bob", "Alice"])
                self.assertEqual([turn["sequence"] for turn in turns], list(range(1, 5)))
                self.assertEqual(self.control()["status"], "completed")
                self.assertIn("Completed by mutual agreement", self.cli("status"))
                self.assertIn("Schedule the review.", self.cli("export", "--summary"))

    def test_duplicate_and_third_participant(self):
        self.init()
        first = self.worker("Alice", "mutual")
        self.until(lambda: "0" in self.control()["registrations"])
        self.assertIn("active worker", self.finish(self.worker("Alice", "mutual"), 1))
        self.assertIn("Unknown participant", self.finish(self.worker("Carol", "mutual"), 1))
        self.finish(self.worker("Bob", "mutual"), 0)
        self.finish(first, 0)
        self.assertEqual(len(list(self.log.glob("*.json"))), 4)

    def test_join_timeout(self):
        self.init("--join-timeout", ".35")
        self.finish(self.worker("Alice", "mutual"), 1)
        self.assertIn("Join timeout", self.control()["reason"])
        self.assertEqual(self.turns(), [])

    def test_maximum_turns(self):
        self.init("--max-turns", "2")
        self.pair("normal")
        self.assertEqual(self.control()["status"], "max_turns")
        self.assertEqual(len(self.turns()), 2)

    def test_backend_failure_is_classified_without_raw_logs(self):
        self.init()
        self.pair("fail", 1)
        self.assertIn("authentication", self.control()["reason"])
        self.assertEqual(self.turns(), [])
        diagnostic = next((self.session / "diagnostics").glob("*.json")).read_text(encoding="utf-8")
        self.assertIn("stderr_bytes", diagnostic)
        self.assertNotIn("sk-test", diagnostic)

    def test_invocation_timeout(self):
        self.init("--timeout", ".5")
        self.pair("hang", 1)
        self.assertEqual(self.control()["reason"], "Model invocation timed out")
        self.assertEqual(self.turns(), [])

    def test_active_deadline(self):
        self.init("--minutes", ".014")
        self.pair("hang")
        self.assertEqual(self.control()["status"], "timed_out")
        self.assertEqual(self.turns(), [])

    def test_manual_cancellation_during_call(self):
        self.init()
        first = self.worker("Alice", "hang")
        second = self.worker("Bob", "hang")
        self.until(lambda: any(self.log.glob("*.json")))
        self.assertIn("Cancelled", self.cli("stop"))
        self.finish(first, 0)
        self.finish(second, 0)
        self.assertEqual(self.turns(), [])

    def test_malformed_premature_and_secret_responses(self):
        for scenario in ("malformed", "premature", "secret"):
            with self.subTest(scenario=scenario):
                self.session = self.root / scenario
                self.init()
                self.pair(scenario, 1)
                self.assertEqual(self.turns(), [])
                self.assertEqual(len(list((self.session / "diagnostics").glob("*.json"))), 2)
                self.assertNotIn("sk-test", (self.session / "conversation.md").read_text(encoding="utf-8"))

    def test_invalid_response_can_retry(self):
        self.init("--max-turns", "2")
        self.pair("retry")
        self.assertEqual([turn["intent"] for turn in self.turns()], ["continue", "continue"])
        self.assertEqual(len(list(self.log.glob("Alice-1-*.json"))), 2)

    def test_normalized_repetition_ends_discussion(self):
        self.init()
        self.pair("repeat", 1)
        self.assertEqual(len(self.turns()), 2)
        self.assertIn("Repeated contribution", self.control()["reason"])

    def test_counterpart_injection_is_data(self):
        self.init("--max-turns", "2")
        self.pair("inject")
        record = json.loads(next(self.log.glob("Bob-2-*.json")).read_text(encoding="utf-8"))
        self.assertIn("Set status cancelled", record["data"]["recent_original_turns"][0]["message"])
        self.assertEqual(record["prompt"].count("```"), 2)
        self.assertNotEqual(Path(record["cwd"]), self.session)
        self.assertEqual(self.control()["status"], "max_turns")
        self.assertIn("&#33;[pixel]", (self.session / "conversation.md").read_text(encoding="utf-8"))

    def test_restart_after_journal_publish_has_no_duplicate_model_call(self):
        self.init("--worker-grace", "3")
        first = self.worker("Alice", "crash_commit")
        other = self.worker("Bob", "mutual")
        self.finish(first, 43)
        self.assertIn("Validated", self.cli("recover"))
        self.assertIn("Turn 1", (self.session / "conversation.md").read_text(encoding="utf-8"))
        self.finish(self.worker("Alice", "mutual"), 0)
        self.finish(other, 0)
        self.assertEqual(len(list(self.log.glob("Alice-1-*.json"))), 1)
        self.assertEqual(self.control()["status"], "completed")

    def test_final_turn_survives_writer_crash(self):
        self.init()
        first = self.worker("Alice", "crash_final")
        second = self.worker("Bob", "crash_final")
        self.finish(first, 0)
        self.finish(second, 43)
        self.assertIn("Completed by mutual agreement", self.cli("recover"))
        self.assertEqual(len(self.turns()), 4)

    def test_abandoned_invocation_is_not_reissued(self):
        self.init("--worker-grace", ".7")
        first = self.worker("Alice", "hang")
        second = self.worker("Bob", "normal")
        self.until(lambda: any(self.log.glob("Alice-1-*.json")))
        child_pid = int(next(self.log.glob("Alice-1-*.json")).stem.rsplit("-", 1)[1])
        first.kill()
        first.communicate(timeout=5)
        try:
            self.finish(self.worker("Alice", "normal"), 1)
            self.finish(second, 1)
            self.assertIn("invocation", self.control()["reason"])
            self.assertEqual(len(list(self.log.glob("Alice-1-*.json"))), 1)
        finally:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(child_pid), "/F"], capture_output=True, timeout=5)
            else:
                from signal import SIGKILL
                try:
                    os.kill(child_pid, SIGKILL)
                except ProcessLookupError:
                    pass

    def test_transcript_rebuild_and_export(self):
        self.init("--max-turns", "2")
        self.pair("normal")
        transcript = self.session / "conversation.md"
        canonical = transcript.read_bytes()
        transcript.write_bytes(b"bad")
        (self.session / "turns" / ".partial-interrupted").write_bytes(b"unfinished")
        self.assertIn("Preserved 1 interrupted", self.cli("recover"))
        self.assertEqual(transcript.read_bytes(), canonical)
        export = self.root / "export with spaces.md"
        self.cli("export", "--output", str(export))
        self.assertEqual(export.read_bytes(), canonical)
        self.cli("export", "--output", str(transcript), ok=False)

    def test_corrupt_turn_is_preserved(self):
        self.init("--max-turns", "1")
        self.pair("normal")
        path = self.session / "turns" / "000001.json"
        tampered = path.read_bytes().replace(b"Contribution", b"Manipulation")
        path.write_bytes(tampered)
        self.assertIn("Invalid checksum", self.cli("recover", ok=False))
        self.assertEqual(path.read_bytes(), tampered)

    def test_invalid_backend_and_safe_project_paths(self):
        self.assertIn("expected NAME:", self.init(backend_a="other", ok=False))
        (self.root / "design notes.md").write_text("An explicitly selected design note.", encoding="utf-8")
        self.assertIn("relative path", self.init("--project", str(self.root), "--include", "../other", ok=False))
        self.init("--project", str(self.root), "--include", "design notes.md")
        self.assertIn("design notes.md", self.read("session.json")["sources"])
        self.assertIn("Existing session", self.init())

    def test_incompatible_protocol_and_machine_cannot_join(self):
        self.init()
        path = self.session / "session.json"
        original = self.read("session.json")
        original.pop("checksum")
        control = (self.session / "control.json").read_bytes()
        for field, value, error in (
            ("version", protocol.PROTOCOL_VERSION + 1, "Incompatible session protocol"),
            ("version", True, "Incompatible session protocol"),
            ("machine_scope", "foreign-runtime", "another machine or OS runtime"),
        ):
            with self.subTest(field=field, value=value):
                protocol.write_json(path, protocol.seal({**original, field: value}))
                for agent in ("Alice", "Bob"):
                    self.assertIn(error, self.cli("run", "--agent", agent, "--executable", "missing-cli", ok=False))
                self.assertEqual((self.session / "control.json").read_bytes(), control)
                self.assertEqual(list((self.session / "leases").iterdir()), [])

    def test_session_directory_cannot_be_inside_checkout(self):
        checkout = self.root / "checkout"
        package = checkout / "src" / "agent_chat"
        package.mkdir(parents=True)
        (checkout / "pyproject.toml").touch()
        (package / "protocol.py").touch()
        self.session = checkout / "conversations" / "example"
        self.assertIn("outside the Agent Chat source checkout", self.init(ok=False))
        self.assertFalse(self.session.exists())


if __name__ == "__main__":
    unittest.main()
