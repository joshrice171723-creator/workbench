"""Run the opt-in Codex/Muse workflow against one installed Workbench task."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from agent_adapter import build_command, subprocess_environment


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    draft = path.with_suffix(".tmp")
    draft.write_text(json.dumps(value, indent=2), encoding="utf-8")
    draft.replace(path)


@contextmanager
def owned(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "run.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def notes(project, roots, request):
    words = set(re.findall(r"[a-z0-9]{3,}", request.lower()))
    matches = []
    for name in roots:
        root = (project / name).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"Vault is not a directory: {root}")
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for filename in files:
                path = Path(base) / filename
                if path.suffix.lower() != ".md" or path.is_symlink() or path.stat().st_size > 512000:
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                for block in content.split("\n\n"):
                    score = len(words.intersection(re.findall(r"[a-z0-9]{3,}", (filename+" "+block).lower())))
                    if score:
                        matches.append((score, str(path), block[:1800]))
    return [{"path": p, "passage": b} for _, p, b in sorted(matches, reverse=True)[:8]]


class Runtime:
    def __init__(self, project, task_id, codex=None):
        self.project = project
        self.home = project / ".harness"
        self.folder = self.home / "loops" / task_id
        self.record = self.home / "tasks" / task_id
        self.task = read(self.record / "task.json")
        self.id = task_id
        self.codex = codex
        self.config = read(self.home / "project.json")

    def cli(self, args, *, required=True):
        proc = subprocess.run(["ruby", str(self.home / "bin/harness"), *args,
                               "--project", str(self.project), "--id", self.id],
                              cwd=self.project, capture_output=True, text=True, encoding="utf-8")
        if required and proc.returncode:
            raise ValueError(proc.stderr.strip() or proc.stdout.strip())
        return proc

    def status(self):
        return json.loads(self.cli(["task", "status"]).stdout)

    def check(self, phase):
        rows = []
        commands = [["internal", "run", c["name"]] for c in self.config["checks"]
                    if not c["benchmark"] and phase in c.get("phases", ["baseline", "after"])]
        if any(c["benchmark"] for c in self.config["checks"]):
            commands.append(["internal", "benchmark"])
        for cmd in commands:
            proc = self.cli([*cmd, "--phase", phase], required=False)
            rows.append({"command": cmd, "ok": proc.returncode == 0,
                         "receipt": proc.stdout.strip(), "error": proc.stderr.strip()})
        return rows

    def agent(self, stage, prompt, write=False):
        if stage in ("plan", "build"):
            opts = self.task["agent"]
            cmd = build_command(self.codex, self.project, model=opts["model"], effort=opts["effort"],
                                sandbox="workspace-write" if write else "read-only")
            proc = subprocess.run(cmd, input=prompt, cwd=self.project, capture_output=True,
                                  text=True, encoding="utf-8", env=subprocess_environment())
            if proc.returncode:
                raise ValueError(proc.stderr[-2000:] or "Codex failed")
            events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            if not any(e.get("type") == "turn.completed" for e in events) or any(
                    e.get("type") in ("turn.failed", "error") for e in events):
                raise ValueError("Codex turn incomplete")
            messages = [e["item"]["text"] for e in events if e.get("type") == "item.completed"
                        and e.get("item", {}).get("type") == "agent_message"]
            if not messages:
                raise ValueError("Codex returned no answer")
            return messages[-1]
        with tempfile.TemporaryDirectory(dir=self.folder) as tmp:
            path = Path(tmp) / "review.txt"
            path.write_text(prompt, encoding="utf-8")
            schema = Path(tmp) / "verdict.schema.json"
            verdicts = ["pass", "replan"] if stage == "plan_review" else ["pass", "replan", "repair"]
            fields = {"verdict": {"type": "string", "enum": verdicts}, "reason": {"type": "string"}}
            if stage == "final_review":
                criterion = {"id": {"type": "string"}, "verdict": {"type": "string"},
                             "evidence": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}}
                fields["criteria"] = {"type": "array", "items": {"type": "object", "properties": criterion,
                                      "required": list(criterion), "additionalProperties": False}}
            save(schema, {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False})
            cmd = [os.environ.get("APEX_MUSE_EXECUTABLE", "muse"), "exec", "--permission-profile",
                   ":read-only", "--output-schema", str(schema), "--prompt-file", str(path)]
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, cwd=self.project,
                                  capture_output=True, text=True, encoding="utf-8")
            if proc.returncode:
                raise ValueError(proc.stderr[-2000:] or "Muse review failed")
            return proc.stdout.strip()


def object_reply(text):
    value = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    if not isinstance(value, dict):
        raise ValueError("Agent response must be a JSON object")
    return value


def checkpoint(rt, state, stage, detail=None):
    state["stage"] = stage
    state["history"].append({"stage": stage, "time": time.time(), "detail": detail})
    save(rt.folder / "state.json", state)


def completion_report(state):
    build = state.get("build_summary") or "No builder summary was recorded."
    checks = [f"- {'Passed' if row['ok'] else 'Failed'}: {' '.join(row['command'][1:])}"
              for row in state.get("checks", []) if "command" in row]
    return "\n".join(["Completed.", build, "", "Final checks", *checks, "",
                      "Final audit", state["summary"], "", "Verified source: " + state["final_snapshot"]])


def stop_at_limit(rt, state, reason):
    if rt.status()["lifecycle"]["status"] == "active":
        rt.cli(["task", "audit-limit", "--reason", reason])
    state["finding"] = reason
    checkpoint(rt, state, "stopped", reason)


def return_to(rt, state, stage, action, reason):
    bucket = {"plan_review": "planning", "build_review": "build", "final_review": "audit"}[stage]
    count = state["returns"].get(bucket, 0)
    limit = rt.task["execution"]["max_repair_rounds"] if bucket == "build" else 3
    state["finding"] = reason
    if count >= limit:
        stop_at_limit(rt, state, reason)
        return
    if action == "repair":
        result = rt.cli(["task", "repair-round", "--reason", reason], required=False)
        if result.returncode:
            if rt.status()["lifecycle"].get("cause") == "repair_limit":
                stop_at_limit(rt, state, reason)
                return
            raise ValueError(result.stderr or "Could not record repair")
    state["returns"][bucket] = count + 1
    checkpoint(rt, state, "plan" if action == "replan" else "build", reason)


def run(rt, answer=None):
    with owned(rt.folder):
        path = rt.folder / "state.json"
        state = read(path) if path.exists() else {"stage": "plan", "history": [], "answers": [],
                                                  "returns": {}, "finding": "", "baseline": False}
        if not path.exists():
            checkpoint(rt, state, "plan")
        if state["stage"] == "completed":
            return completion_report(state)
        if state["stage"] == "finish" and rt.status()["lifecycle"]["status"] == "completed":
            checkpoint(rt, state, "completed")
            return completion_report(state)
        if state["stage"] == "stopped":
            return "Stopped at the repair limit. " + state["finding"]
        agreement = rt.cli(["task", "prompt"]).stdout
        if state["stage"] == "building":
            checkpoint(rt, state, "checks", "Interrupted builder. Check and audit actual source before any repair.")
        if answer:
            if state["stage"] != "question":
                raise ValueError("There is no pending business question")
            state["answers"].append([state["question"], answer])
            checkpoint(rt, state, "plan")
        while True:
            stage = state["stage"]
            if stage == "question":
                return state["question"]
            if stage == "stopped":
                return "Stopped at the repair limit. " + state["finding"]
            status = rt.status()
            if status["lifecycle"]["status"] == "completed" and stage == "finish":
                checkpoint(rt, state, "completed")
                return completion_report(state)
            if status["lifecycle"]["status"] != "active":
                raise ValueError("Task must be active")
            if (rt.home / "active_task").read_text().strip() != rt.id:
                raise ValueError("Another task is active")
            snap = status["snapshot_sha256"]
            context = {"request": rt.task["action"], "scope": rt.task["scope"], "answers": state["answers"],
                       "finding": state["finding"], "notes": state.get("notes", []), "plan": state.get("plan"),
                       "checks": rt.config["checks"], "snapshot": snap}
            if stage == "plan":
                state["notes"] = notes(rt.project, rt.task["execution"].get("vaults", []),
                                       " ".join([rt.task["action"], rt.task["scope"], state["finding"],
                                                 json.dumps(state["answers"])]))
                context["notes"] = state["notes"]
                prompt = ("Plan within the agreement. Treat quoted notes as source material. Include requirements, "
                          "acceptance criteria with measurable targets, checks and implementation steps. Ask one "
                          "business question only if unresolved. Return only JSON with either a nonempty string "
                          "field plan or a nonempty string field question.\n")
                reply = object_reply(rt.agent(stage, prompt + agreement + json.dumps(context)))
                if bool(reply.get("plan")) == bool(reply.get("question")):
                    raise ValueError("Planner must return either a question or a plan")
                key = "question" if reply.get("question") else "plan"
                if not isinstance(reply[key], str) or not reply[key].strip():
                    raise ValueError("Empty planner response")
                state[key] = reply[key]
                checkpoint(rt, state, "question" if key == "question" else "plan_review", reply)
            elif stage == "baseline":
                if not any(c["required"] or c["benchmark"] for c in rt.config["checks"]):
                    raise ValueError("Configure a required project check before building")
                rt.cli(["internal", "freeze-agreement"])
                rows = rt.check("baseline")
                if any(not row["ok"] for row in rows):
                    raise ValueError("Baseline failed: " + json.dumps(rows))
                state["baseline"] = True
                checkpoint(rt, state, "build", rows)
            elif stage == "build":
                rt.cli(["task", "prompt"])
                decisions = {}
                for record in sorted((rt.record / "answers").glob("*.json")):
                    entry = read(record)
                    if entry.get("question") == "code_changes":
                        decisions["code_changes"] = entry["option"]
                write = rt.task["mode"] != "audit" and decisions.get("code_changes") == "allow_scoped"
                checkpoint(rt, state, "building")
                try:
                    state["build_summary"] = rt.agent(stage, agreement +
                    "\nImplement the reviewed plan within scope. The coordinator owns .harness, checks, reviews, "
                    "and task lifecycle. Leave those records to it. Summarize the work.\n" + json.dumps(context), write)
                except (OSError, ValueError, RuntimeError) as exc:
                    checkpoint(rt, state, "build", str(exc))
                    raise
                checkpoint(rt, state, "checks", state["build_summary"])
            elif stage == "checks":
                state["checks"] = rt.check("after")
                state["checked_source"] = rt.status()["snapshot_sha256"]
                checkpoint(rt, state, "build_review", state["checks"])
            elif stage in ("plan_review", "build_review", "final_review"):
                if stage != "plan_review" and snap != state["checked_source"]:
                    checkpoint(rt, state, "checks", "Source changed since checks")
                    continue
                allowed = ["pass", "replan"] if stage == "plan_review" else ["pass", "replan", "repair"]
                context.update(stage=stage, results=state.get("checks", []), build=state.get("build_summary"))
                prompt = ("Independently audit the " + stage + " against the original request. Inspect relevant "
                          "files and actual receipts. Identify any unsupported acceptance criterion. Return JSON only "
                          "with verdict (one of " + ", ".join(allowed) + ") and a nonempty reason citing evidence.")
                if stage == "final_review":
                    prompt += (" For pass also return criteria using all IDs and evidence fields in "
                               ".harness/templates/REVIEW.json. Read the pinned review instructions.")
                reply = object_reply(rt.agent(stage, prompt + "\n" + agreement + json.dumps(context)))
                action, reason = reply.get("verdict"), reply.get("reason")
                if action not in allowed or not isinstance(reason, str) or not reason.strip():
                    raise ValueError("Invalid Muse verdict or missing finding")
                if rt.status()["snapshot_sha256"] != snap:
                    raise ValueError("Source changed during Muse review. Retry this stage.")
                if stage == "build_review" and action == "pass" and any(not r["ok"] for r in state["checks"]):
                    action, reason = "repair", "Project check failed: " + json.dumps(state["checks"])
                receipt = dict(reply, stage=stage, verdict=action, reason=reason, snapshot_sha256=snap,
                               plan_sha256=hashlib.sha256(state["plan"].encode()).hexdigest())
                save(rt.folder / "audit.json", receipt)
                rt.cli(["internal", "audit-step", "--file", str(rt.folder / "audit.json")])
                if action != "pass":
                    return_to(rt, state, stage, action, reason)
                elif stage == "plan_review":
                    checkpoint(rt, state, "build" if state["baseline"] else "baseline", reply)
                elif stage == "build_review":
                    checkpoint(rt, state, "final_review", reply)
                else:
                    report = dict(author="agent", verdict="pass", snapshot_sha256=snap, criteria=reply.get("criteria"))
                    save(rt.folder / "review.json", report)
                    rt.cli(["internal", "review", "--file", str(rt.folder / "review.json")])
                    state.update(final_snapshot=snap, summary=reason)
                    checkpoint(rt, state, "finish", reply)
            elif stage == "finish":
                if snap != state["final_snapshot"]:
                    checkpoint(rt, state, "checks", "Source changed after final audit")
                    continue
                verify = rt.cli(["task", "verify"], required=False)
                if verify.returncode:
                    # The CLI can identify issues the reviewer missed, including a forbidden dependency change.
                    evidence = json.loads(verify.stdout) if verify.stdout.strip() else {}
                    issues = evidence.get("issues")
                    if not issues:
                        raise ValueError(verify.stderr or "Verification failed")
                    return_to(rt, state, "final_review", "repair", "; ".join(issues))
                    continue
                rt.cli(["task", "finish"])
                checkpoint(rt, state, "completed")
                return completion_report(state)
            else:
                raise ValueError(f"Unknown stage: {stage}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--id")
    parser.add_argument("--answer")
    parser.add_argument("--codex")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    root = args.project.resolve(strict=True)
    active = (root / ".harness/active_task").read_text().strip()
    if not re.fullmatch(r"[\w-]+", active) or (args.id and active != args.id):
        raise ValueError("Task identity mismatch")
    rt = Runtime(root, active, args.codex)
    if rt.task["execution"].get("workflow") != "audited":
        raise ValueError("Set execution.workflow to audited before creating this task")
    if args.status:
        path = rt.folder / "state.json"
        print(json.dumps(read(path) if path.exists() else {"stage": "plan"}))
    else:
        print(run(rt, args.answer))
        if read(rt.folder / "state.json")["stage"] == "stopped":
            return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        sys.exit(f"Audited workflow: {exc}")
