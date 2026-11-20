"""Scripted route coverage. Providers are controlled; no model credentials needed."""
import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest

from audited_flow import read, run


class FakeRuntime:
    def __init__(self, root):
        self.id = "one"
        self.project = self.home = root
        self.folder = root / "loops/one"
        self.record = root / "tasks/one"
        self.record.mkdir(parents=True)
        (root / "active_task").write_text(self.id)
        self.task = {"action": "clarify reply", "scope": "matching", "mode": "feature",
                     "execution": {"max_repair_rounds": 3}}
        self.config = {"checks": [{"required": True}]}
        self.calls = []
        self.routes = []
        self.source = 0
        self.question = False
        self.fail_check = False
        self.change_source = False
        self.finished = False

    def cli(self, args, required=True):
        if args[:2] == ["task", "finish"]:
            self.finished = True
        return CompletedProcess(args, 0, stdout="agreement", stderr="")

    def status(self):
        return {"snapshot_sha256": str(self.source), "lifecycle": {"status": "active"}}

    def agent(self, stage, prompt, write=False):
        self.calls.append(stage)
        if stage == "build":
            self.source += 1
            return "updated"
        if stage == "plan":
            if self.question:
                self.question = False
                return '{"question":"Which clinic?"}'
            return '{"plan":"Check ambiguous and unambiguous replies"}'
        if self.change_source and stage == "final_review":
            self.change_source = False
            self.source += 1
        if self.routes and self.routes[0][0] == stage:
            _, result = self.routes.pop(0)
            if result == "interrupt":
                raise OSError("provider unavailable")
            if result == "malformed":
                return "unparseable"
            return json.dumps({"verdict": result, "reason": "specific issue"})
        return '{"verdict":"pass","reason":"Evidence matches","criteria":[]}'

    def check(self, phase):
        ok = phase != "after" or not self.fail_check
        if phase == "after":
            self.fail_check = False
        return [{"ok": ok}]


class Routes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rt = FakeRuntime(Path(tmp.name))

    def test_build_audit_reopens_planning_then_final_returns_to_build(self):
        self.rt.routes = [("build_review", "replan"), ("final_review", "repair")]
        run(self.rt)
        self.assertEqual(self.rt.calls, ["plan", "plan_review", "build", "build_review",
                         "plan", "plan_review", "build", "build_review", "final_review",
                         "build", "build_review", "final_review"])
        self.assertTrue(self.rt.finished)

    def test_final_replan_and_plan_rejection(self):
        self.rt.routes = [("plan_review", "replan"), ("final_review", "replan")]
        run(self.rt)
        self.assertEqual(self.rt.calls.count("plan"), 3)
        self.assertEqual(self.rt.calls.count("final_review"), 2)

    def test_failed_checks_override_passing_review(self):
        self.rt.fail_check = True
        run(self.rt)
        self.assertEqual(self.rt.calls.count("build_review"), 2)
        self.assertEqual(self.rt.calls.count("final_review"), 1)

    def test_question_and_interrupted_review_resume(self):
        self.rt.question = True
        self.assertEqual(run(self.rt), "Which clinic?")
        self.rt.routes = [("final_review", "interrupt")]
        with self.assertRaises(OSError):
            run(self.rt, "Austin")
        self.assertFalse(self.rt.finished)
        run(self.rt)
        self.assertEqual(self.rt.calls.count("build"), 1)
        self.assertEqual(read(self.rt.folder / "state.json")["answers"], [["Which clinic?", "Austin"]])

    def test_invalid_review_or_source_drift_cannot_finish(self):
        self.rt.routes = [("build_review", "malformed")]
        with self.assertRaises(ValueError):
            run(self.rt)
        self.rt.change_source = True
        with self.assertRaisesRegex(ValueError, "Source changed during"):
            run(self.rt)
        self.assertFalse(self.rt.finished)
        run(self.rt)
        self.assertEqual(self.rt.calls.count("build_review"), 3)

    def test_limit_stops_without_finish(self):
        self.rt.routes = [("plan_review", "replan")] * 4
        self.assertIn("Stopped", run(self.rt))
        self.assertFalse(self.rt.finished)
        self.assertEqual(self.rt.calls.count("plan"), 4)


class InstalledWorkflow(unittest.TestCase):
    """Use the actual Ruby agreement, source fingerprints, audit ledger, and finish gates."""
    def test_existing_and_new_project(self):
        import shutil
        import subprocess
        from unittest.mock import patch
        from audited_flow import Runtime, save
        if not shutil.which("ruby"):
            self.skipTest("Requires the Ruby Workbench runtime")
        harness = Path(__file__).resolve().parent / "bin/harness"
        for mode in ["build", "feature"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                def cli(*args, check=True):
                    return subprocess.run(["ruby", str(harness), *args, "--project", tmp],
                                          text=True, capture_output=True, check=check)
                if mode == "feature":
                    (project / "code.rb").write_text("VALUE = 1")
                cli("setup", "init")
                home = project / ".harness"
                config = read(home / "project.json")
                config["execution"]["workflow"] = "audited"
                config["checks"] = [dict(name="behavior", required=True, benchmark=False, phases=["after"],
                                         argv=["ruby", "-e", "load './code.rb'; abort unless VALUE == 2"])]
                save(home / "settings.json", config)
                cli("setup", "configure", "--file", str(home / "settings.json"))
                save(home / "answers.json", dict(scope_confirmed="confirmed", code_changes="allow_scoped",
                     migrations="deny", dependencies="deny", engine_integration="confirmed"))
                tid = json.loads(cli(mode, "--action", "Set value to two", "--scope", "code.rb", "--file",
                                     str(home / "answers.json")).stdout)["task_id"]
                rt = Runtime(project, tid)
                criteria = [dict(id=c["id"], verdict="pass", evidence=["Inspected code.rb and behavior receipt"])
                            for c in read(home / "templates/REVIEW.json")["criteria"]]
                def reply(stage, prompt, write=False):
                    if stage == "plan":
                        return '{"plan":"Set value to 2 and verify behavior"}'
                    if stage == "build":
                        self.assertTrue(write)
                        (project / "code.rb").write_text("VALUE = 2")
                        return "Changed code.rb"
                    self.assertFalse(write)
                    self.assertNotEqual(cli("task", "finish", check=False).returncode, 0)
                    return json.dumps(dict(verdict="pass", reason="Verified the value", criteria=criteria))
                with patch.object(rt, "agent", side_effect=reply):
                    self.assertIn("Completed", run(rt))
                self.assertEqual(rt.status()["lifecycle"]["status"], "completed")
                history = json.loads(cli("runs", "show", tid).stdout)
                self.assertEqual([r["stage"] for r in history["audits"]], ["plan_review","build_review","final_review"])
                self.assertEqual(history["ledger"]["verdict"], "pass")
