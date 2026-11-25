import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_chat import protocol, runner


class SourceSelectionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.session = self.root / "session"

    def initialize(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "agent_chat", "init", "--dir", str(self.session),
             "--a", "A:codex", "--b", "B:grok", "--objective", "Synthetic test.",
             "--project", str(self.project), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )

    def test_range_from_large_file_keeps_exact_text_and_provenance(self):
        path = "design notes.md"
        exact = "beta Δ two\r\ngamma-three"
        document = ("H" * 25_000 + "\r\n" + exact).encode("utf-8")
        (self.project / path).write_bytes(document)

        result = self.initialize("--range", f"{path}=2-3")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = protocol.Store(self.session).load()
        key = f"{path}#L2-L3"
        expected = {"path": path, "start": 2, "end": 3, "bytes": len(exact.encode("utf-8"))}
        self.assertEqual(config["sources"], {key: exact})
        self.assertEqual(config["source_provenance"], {key: expected})
        for slot in (0, 1):
            data = prompt_data(runner.build_prompt(config, slot, []))
            self.assertEqual(data["selected_project_extracts"], {key: exact})
            self.assertEqual(data["source_provenance"], {key: expected})

    def test_out_of_file_and_conflicting_selectors_fail(self):
        (self.project / "notes.md").write_bytes(b"only\r\n")
        for args, reason in (
            (("--range", "notes.md=1-2"), "extends past end of file"),
            (("--range", "notes.md=2-2"), "starts past end of file"),
            (("--range", "notes.md=2-1"), "positive, ascending line numbers"),
            (("--include", "notes.md", "--range", "notes.md=1-1"), "either --include or --range"),
        ):
            with self.subTest(args=args):
                result = self.initialize(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stderr)
                self.assertFalse((self.session / "session.json").exists())

    def test_legacy_include_loads_without_provenance(self):
        (self.project / "notes.md").write_bytes(b"legacy body\n")
        result = self.initialize("--include", "notes.md")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = protocol.Store(self.session).load()
        self.assertEqual(config["version"], 1)
        self.assertEqual(config["sources"], {"notes.md": "legacy body\n"})
        self.assertNotIn("source_provenance", config)

    def test_selection_and_scan_byte_boundaries(self):
        path = self.project / "notes.md"
        selection = b"s" * 23_999 + b"\n"
        skipped = b"p" * 1_048_575 + b"\n"
        path.write_bytes(skipped + selection + b"next")
        self.assertEqual(protocol.source_range(path, 2, 2).encode("utf-8"), selection)
        with self.assertRaisesRegex(protocol.ProtocolError, "24,000-byte selection limit"):
            protocol.source_range(path, 2, 3)
        path.write_bytes(b"p" + skipped + selection)
        with self.assertRaisesRegex(protocol.ProtocolError, "1,048,576-byte scan limit"):
            protocol.source_range(path, 2, 2)


def prompt_data(prompt):
    return json.loads(prompt.split("\n```json\n", 1)[1].split("\n```", 1)[0])


class RetryFeedbackTest(unittest.TestCase):
    def test_retry_corrects_each_message_class_without_echoing_the_reply(self):
        config = {
            "objective": "Synthetic test.", "participants": [{"name": "A"}, {"name": "B"}],
            "max_turns": 3, "message_chars": 100, "sources": {}, "context_chars": 8000,
            "timeout": 5, "poll": 0.05,
        }
        rejected_text = "Rejected sample " + ("x" * 85)
        for rejected, diagnostic in (
            (7, "must be text (limit 100 characters)"),
            (" \r\n\t ", "is empty (limit 100 characters)"),
            (f"\n{rejected_text}  ", "101 characters after trimming (limit 100)"),
        ):
            with self.subTest(diagnostic=diagnostic):
                control = {"deadline": time.time() + 5}
                prompts = []

                def invoke(_backend, prompt, *_args):
                    prompts.append(prompt)
                    message = rejected if len(prompts) == 1 else "Corrected reply."
                    return json.dumps({"message": message, "intent": "continue"}), {}

                def resolve(_store, _claim, *, retry=False, **_details):
                    return control, {"attempt": 2} if retry else None

                with (
                    patch.object(runner, "invoke_agent", side_effect=invoke),
                    patch.object(runner, "resolve", side_effect=resolve) as resolutions,
                ):
                    runner.perform(object(), object(), 0, config, control, [], {"attempt": 1})

                self.assertEqual(len(prompts), 2)
                first, second = resolutions.call_args_list
                reason = prompt_data(prompts[1])["previous_attempt_error"]
                self.assertIn(diagnostic, reason)
                self.assertEqual(first.kwargs["error"], reason)
                self.assertTrue(first.kwargs["retry"])
                self.assertNotIn("previous_attempt_error", prompt_data(prompts[0]))
                self.assertNotIn(rejected_text, prompts[1])
                self.assertEqual(second.kwargs["response"]["message"], "Corrected reply.")
