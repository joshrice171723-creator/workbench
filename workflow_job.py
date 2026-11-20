"""Own a coordinator process and its descendants on behalf of Workbench chat."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import uuid


def process_group():
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def terminate_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=8)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class WorkflowJob:
    def __init__(self, project, task_id, message, answer, codex):
        self.id = uuid.uuid4().hex
        self.log_dir = project / ".harness/chat" / task_id / self.id
        self.log_dir.mkdir(parents=True)
        self.record = dict(id=self.id, task_id=task_id, user_message=message, state="starting",
                           started_at=datetime.now(timezone.utc).isoformat(), events=[], next_cursor=0)
        command = [sys.executable, str(Path(__file__).with_name("audited_flow.py")),
                   "--project", str(project), "--id", task_id]
        if answer:
            command += ["--answer", answer]
        if codex:
            command += ["--codex", str(codex)]
        self.write("turn.json")
        self.proc = subprocess.Popen(command, cwd=project, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", **process_group())
        self.record["state"] = "running"
        self.worker = threading.Thread(target=self.collect, daemon=True)
        self.worker.start()

    def write(self, name):
        path = self.log_dir / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.record), encoding="utf-8")
        tmp.replace(path)

    def collect(self):
        out, error = self.proc.communicate()
        cancelled = self.record["state"] == "cancelling"
        state = "cancelled" if cancelled else "failed" if self.proc.returncode else "completed"
        self.record.update(response=out.strip(), error=error.strip(), exit_code=self.proc.returncode, state=state)
        self.write("result.json")

    def cancel(self):
        if self.proc.poll() is not None:
            return False
        self.record["state"] = "cancelling"
        terminate_tree(self.proc)
        return True

    def wait(self, timeout=None):
        self.worker.join(timeout)
        return self.snapshot()

    def snapshot(self, after=0):
        return self.record.copy()
