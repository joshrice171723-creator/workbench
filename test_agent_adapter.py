import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_adapter as adapter


SESSION = "11111111-2222-4333-8444-555555555555"


class FakeProcess:
    def __init__(self, events, code=0):
        self.pid = 123456
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(json.dumps(e) for e in events) + "\n")
        self.stderr = io.StringIO()
        self.code = code

    def wait(self):
        return self.code

    def poll(self):
        return None


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / "codex.exe"
        self.exe.write_text("mock executable")

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **options):
        return adapter.AgentJob("Inspect the synthetic project.", self.root, self.root / "job",
                                executable=self.exe, **options)

    def test_commands_use_stdin_and_explicit_sandbox_without_bypass(self):
        command = adapter.build_command(self.exe, self.root, sandbox="read-only")
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertNotIn("--ephemeral", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(command[-1], "-")

    def test_resume_uses_exact_identifier_without_unsupported_cd_flag(self):
        command = adapter.build_command(self.exe, self.root, thread_id=SESSION)
        self.assertEqual(command[1:3], ["exec", "resume"])
        self.assertEqual(command[-2:], [SESSION, "-"])
        self.assertNotIn("-C", command)

    @unittest.skipUnless(adapter.os.name == "nt", "Windows desktop launch context")
    def test_standalone_child_uses_task_sandbox_for_start_and_resume(self):
        parent = {"CODEX_WINDOWS_REGISTERED_CORE": "1", "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY": "Desktop",
                  "CODEX_PERMISSION_PROFILE": ":danger-full-access"}
        with patch.dict(adapter.os.environ, parent):
            child = adapter.subprocess_environment()
            self.assertFalse(parent.keys() & child.keys())
            self.assertEqual(adapter.os.environ["CODEX_WINDOWS_REGISTERED_CORE"], "1")
        for sandbox in ("read-only", "workspace-write"):
            for session in (None, SESSION):
                argv = adapter.build_command(self.exe, self.root, thread_id=session, sandbox=sandbox)
                self.assertIn('windows.sandbox="elevated"', argv)
                self.assertIn(f'sandbox_mode="{sandbox}"', argv)
                if session is None:
                    self.assertEqual(sandbox, argv[argv.index("--sandbox") + 1])

    def test_invalid_identifiers_and_unsafe_sandbox_are_rejected(self):
        with self.assertRaises(ValueError):
            adapter.build_command(self.exe, self.root, thread_id="--last")
        with self.assertRaises(ValueError):
            adapter.build_command(self.exe, self.root, sandbox="danger-full-access")

    def test_real_protocol_event_shapes_complete_and_paginate(self):
        events = [{"type": "thread.started", "thread_id": SESSION},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "Found the entry point."}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10}}]
        with patch.object(adapter.subprocess, "Popen", return_value=FakeProcess(events)):
            job = self.job().start()
            result = job.wait(5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["thread_id"], SESSION)
        self.assertEqual(result["response"], "Found the entry point.")
        self.assertEqual(len(job.snapshot(after=2)["events"]), 1)
        self.assertTrue((self.root / "job/events.jsonl").is_file())

    def test_exit_zero_without_turn_completion_is_not_a_pass(self):
        with patch.object(adapter.subprocess, "Popen", return_value=FakeProcess([])):
            result = self.job().start().wait(5)
        self.assertEqual(result["state"], "failed")
        self.assertIsNotNone(result["error"])

    def test_authentication_failure_is_visible(self):
        events = [{"type": "error", "message": "Authentication required"}]
        with patch.object(adapter.subprocess, "Popen", return_value=FakeProcess(events, code=1)):
            result = self.job().start().wait(5)
        self.assertEqual(result["state"], "failed")
        self.assertIn("Authentication required", result["error"])

    def test_mismatched_resumed_thread_cannot_succeed(self):
        events = [{"type": "thread.started", "thread_id": "99999999-2222-4333-8444-555555555555"},
                  {"type": "turn.completed"}]
        with patch.object(adapter.subprocess, "Popen", return_value=FakeProcess(events)):
            result = self.job(thread_id=SESSION).start().wait(5)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["thread_id"], SESSION)

    def test_cancellation_targets_only_the_owned_process(self):
        job = self.job()
        job.process = FakeProcess([])
        job.state = "running"
        if adapter.os.name == "nt":
            with patch.object(adapter.subprocess, "run") as terminate:
                self.assertTrue(job.cancel())
            self.assertEqual(terminate.call_args.args[0], ["taskkill", "/PID", "123456", "/T", "/F"])
        else:
            with patch.object(adapter.os, "killpg") as terminate:
                self.assertTrue(job.cancel())
            self.assertEqual(terminate.call_args.args[0], 123456)

    def test_missing_executable_is_an_explicit_capability_failure(self):
        result = adapter.capabilities(self.root / "missing")
        self.assertFalse(result["available"])
        self.assertFalse(result["interactive_command_approval"])


if __name__ == "__main__":
    unittest.main()
