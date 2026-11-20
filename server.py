"""Loopback-only workbench. All mutations require an explicit authenticated POST."""
import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import tempfile
from urllib.parse import parse_qs, urlparse
import uuid

from agent_adapter import AgentJob, capabilities
from chat_workflow import ChatWorkflow
from workflow_job import WorkflowJob

ROOT = Path(__file__).resolve().parent


def read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return fallback


def recorded_lifecycle(task_folder):
    folder = task_folder / "lifecycle"
    try:
        try:
            files = [path for path in folder.iterdir() if path.suffix == ".json"]
        except FileNotFoundError:
            files = []
        if not files:
            return {"status": "active", "reason": "Task started"}
        latest = max(files)
        record = json.loads(latest.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict) or record.get("status") not in ("active", "paused", "stopped", "completed"):
            raise ValueError(f"Invalid task status in {latest.name}")
        return record
    except (OSError, ValueError) as error:
        return {"status": "unverified", "reason": f"Cannot read task lifecycle in {folder}. {error}"}


class Workbench:
    def __init__(self, project=None, executable=None, agent_factory=AgentJob):
        self.project = self.project_path(project) if project else None
        self.executable = executable
        self.agent_factory = agent_factory
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.capability_cache = None

    @staticmethod
    def project_path(value):
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Select a project directory.")
        return root

    def require_project(self):
        if self.project is None:
            raise ValueError("Select a project first.")
        return self.project

    def active(self):
        if not self.project:
            return None
        home = self.project / ".harness"
        try:
            task_id = (home / "active_task").read_text().strip()
        except OSError:
            return None
        if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in task_id):
            raise ValueError("The active task pointer is invalid.")
        task = read_json(home / "tasks" / task_id / "task.json")
        if task is None:
            return None
        task = {key: value for key, value in task.items() if key != "initial_snapshot"}
        task["id"] = task_id
        folder = home / "tasks" / task_id
        answers = {}
        answer_sources = {}
        for path in sorted((folder / "answers").glob("*.json")):
            record = read_json(path, {})
            if "question" in record:
                answers[record["question"]] = record.get("option")
                answer_sources[record["question"]] = record.get("source", {"kind": "unrecorded"})
        task.update(answers=answers, answer_sources=answer_sources, lifecycle=recorded_lifecycle(folder))
        task['status'] = task['lifecycle']['status']
        return task

    def busy(self):
        return any(self.job_snapshot(job)["state"] in {"created", "starting", "running", "cancelling"} for job in self.jobs.values())

    @staticmethod
    def job_snapshot(job, after=0):
        if isinstance(job, dict):
            result = dict(job, events=job.get("events", [])[after:], next_cursor=len(job.get("events", [])))
        else:
            result = job.snapshot(after=after)
        result.update(status=result.get('state'), type=result.get('kind', 'agent'), next_after=result.get('next_cursor', 0))
        return result

    def state(self):
        with self.lock:
            home = self.project / ".harness" if self.project else None
            config = read_json(home / "project.json", {}) if home else {}
            questions = read_json(home / "config/questions.json", {}).get("questions", []) if home else []
            runs = []
            saved_jobs = []
            if home:
                for folder in sorted((home / "tasks").glob("*"), reverse=True):
                    record = read_json(folder / "task.json")
                    if record:
                        record.pop('initial_snapshot', None)
                        record['lifecycle'] = recorded_lifecycle(folder)
                        record['status'] = record['lifecycle']['status']
                        runs.append(record)
                live_paths = {job.log_dir for job in self.jobs.values() if not isinstance(job, dict)}
                for folder in sorted((home / 'chat').glob('*/*')):
                    if not folder.is_dir() or folder in live_paths:
                        continue
                    saved = read_json(folder / 'result.json')
                    if saved:
                        saved.setdefault('task_id', folder.parent.name)
                        saved.update(status=saved.get('state'), type='agent', restored=True, events=[])
                        saved_jobs.append(saved)
                    elif (folder / 'events.jsonl').is_file():
                        turn = read_json(folder / 'turn.json', {})
                        saved_jobs.append({**turn, 'id': turn.get('id', folder.name), 'task_id': folder.parent.name, 'state': 'unverified', 'status': 'unverified', 'type': 'agent',
                            'restored': True, 'events': [], 'error': 'The server no longer owns this process. Its completion is unverified.'})
            live_jobs = []
            for job in self.jobs.values():
                item = self.job_snapshot(job)
                item['events'] = []
                live_jobs.append(item)
            workflow = ChatWorkflow(self).state() if self.project else {"chat_intake": None, "workflow_messages": []}
            return {**workflow, "project": {'path': str(self.project), 'label': self.project.name, 'exists': True} if self.project else None, "installed": bool(home and (home / "project.json").is_file()),
                    "config": config, "active_task": self.active(), "runs": runs, "questions": questions,
                    "jobs": live_jobs + saved_jobs,
                    "capabilities": self.capability_cache or {"available": None, "note": "Check agent availability explicitly."},
                    "csrf_token": self.token}

    def cli(self, args, timeout=120):
        project = self.require_project()
        ruby = shutil.which("ruby")
        if not ruby:
            raise ValueError("Ruby was not found on PATH. Install the project's Ruby runtime.")
        installed = project / ".harness/bin/harness"
        script = installed if installed.is_file() else ROOT / "bin/harness"
        result = subprocess.run([ruby, str(script), *args, "--project", str(project)], cwd=project,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        output = result.stdout + result.stderr
        parsed = None
        try:
            parsed = json.loads(result.stdout)
        except ValueError:
            pass
        return {"exit_code": result.returncode, "output": output, "data": parsed}

    def require_success(self, result):
        if result["exit_code"]:
            raise ValueError(result["output"] or "The project command failed.")
        return result

    def cli_with_json(self, args, data):
        with tempfile.NamedTemporaryFile(dir=self.require_project() / ".harness",
                mode="w", suffix=".json", delete=False, encoding="utf-8") as draft:
            path = Path(draft.name)
            json.dump(data, draft)
        try:
            return self.require_success(self.cli([*args, "--file", str(path)]))
        finally:
            path.unlink(missing_ok=True)

    def command_job(self, args):
        job_id = str(uuid.uuid4())
        task = self.active()
        if not task:
            raise ValueError("Create a task before running a check.")
        if task["status"] == "unverified":
            raise ValueError(task["lifecycle"]["reason"])
        record = {"id": job_id, "task_id": task["id"], "started_at": datetime.now(timezone.utc).isoformat(),
                  "kind": "command", "state": "running", "events": [], "response": "", "error": None}
        self.jobs[job_id] = record
        def execute():
            try:
                result = self.cli([*args, "--id", task["id"]], timeout=600)
                with self.lock:
                    record.update(result, state="completed" if result["exit_code"] == 0 else "failed", response=result["output"])
            except Exception as error:
                with self.lock:
                    record.update(state="failed", error=str(error))
        self.pool.submit(execute)
        return dict(record)

    def post(self, route, payload):
        with self.lock:
            if route == "/api/chat/cancel":
                job = self.jobs.get(payload.get("id"))
                if not job or isinstance(job, dict):
                    raise ValueError("Select an owned agent job to cancel.")
                return {"cancel_requested": job.cancel()}
            if route == '/api/task/action' and payload.get('action') in {'pause', 'stop'}:
                for job in self.jobs.values():
                    if not isinstance(job, dict) and job.snapshot()['state'] in {'starting', 'running'}:
                        job.cancel()
                        job.wait(10)
            if self.busy():
                raise ValueError("A job is active. Wait for it or cancel its owned agent process.")
            if route == "/api/project":
                self.project = self.project_path(payload["path"])
                self.jobs = {}
                self.capability_cache = None
                return self.state()
            if route == "/api/capabilities":
                self.capability_cache = capabilities(self.executable, self.project)
                return self.capability_cache
            self.require_project()
            if route == "/api/setup":
                action = payload.get("action")
                if action == "install":
                    return self.require_success(self.cli(["setup", "init"]))
                if action == "discover":
                    return self.require_success(self.cli(["setup", "discover"]))
                if action == "inspect-engine":
                    return self.cli_with_json(["setup", "inspect-engine"], payload["engine"])
                if action == "verify-adapter":
                    pin = self.require_success(self.cli(["setup", "check-engine"]))["data"]
                    response = ChatWorkflow(self).route("/help")
                    home = self.project / ".harness"
                    expected = (home / "config/help.txt").read_text(encoding="utf-8")
                    if response.get("kind") != "fixed_help" or response.get("response") != expected:
                        raise ValueError("The built-in chat route returned different help from the installed CLI.")
                    guide = home / "docs/AGENT_ADAPTER.md"
                    if not guide.is_file():
                        raise ValueError("The installed agent guide is missing.")
                    return {"fixed_help": "matched", "engine": pin["engine"], "guide": str(guide.relative_to(self.project)),
                            "live_model": "A task is required to verify a live model turn."}
                if action == "configure":
                    config = payload.get("config")
                    if not isinstance(config, dict):
                        raise ValueError("Supply a project configuration object.")
                    return self.cli_with_json(["setup", "configure"], config)
                raise ValueError("Unknown setup action.")
            if route == "/api/task":
                mode = payload.get("mode")
                if mode not in {"audit", "refactor", "feature", "build"}:
                    raise ValueError("Choose a supported task mode.")
                result = self.cli_with_json([mode, "--answer-channel", "form", "--action", str(payload.get("action", "")),
                    "--scope", str(payload.get("scope", ""))], payload.get("answers", {}))
                return {"created": result, "state": self.state()}
            if route == "/api/answer":
                return self.require_success(self.cli(["answer", str(payload["question"]), str(payload["option"]), "--answer-channel", "form"]))
            if route == "/api/task/action":
                action = payload.get("action")
                if action not in {"pause", "stop", "resume", "verify", "finish", "report", "repair-round"}:
                    raise ValueError("Unknown task action.")
                args = ["task", action]
                if payload.get("id"):
                    args += ["--id", str(payload["id"])]
                if payload.get("reason"):
                    args += ["--reason", str(payload["reason"])]
                return self.cli(args)
            if route in {"/api/check", "/api/benchmark"}:
                phase = payload.get("phase")
                if phase not in {"baseline", "after"}:
                    raise ValueError("Choose baseline or after.")
                args = ["benchmark", "--phase", phase] if route.endswith("benchmark") else ["run", str(payload["name"]), "--phase", phase]
                return self.command_job(args)
            if route == "/api/runs/compare":
                ids = payload.get("ids", [])
                if not isinstance(ids, list) or len(ids) != 2:
                    raise ValueError("Select exactly two run IDs.")
                return self.cli(["runs", "compare", *map(str, ids)])
            if route == "/api/chat":
                message = payload.get("message", "")
                if not isinstance(message, str) or not message.strip() or len(message) > 64000:
                    raise ValueError("Supply a message of 1 to 64000 characters.")
                routed = ChatWorkflow(self).route(message)
                task = self.active()
                if task and task.get("execution", {}).get("workflow") == "audited" and task["status"] == "active":
                    agreed = routed and routed.get("response", "").startswith("Agreement recorded.")
                    if routed is None or agreed:
                        self.require_success(self.cli(["task", "prompt", "--id", task["id"]]))
                        folder = self.project / ".harness/loops" / task["id"]
                        state = read_json(folder / "state.json", {})
                        answer = message if state.get("stage") == "question" else None
                        if not agreed and not answer and message.strip() not in {"/run", "/continue"}:
                            return ChatWorkflow(self).reply(message, "Send /run to start or continue the audited task.",
                                                            task["id"])
                        job = WorkflowJob(self.project, task["id"], message, answer, self.executable)
                        self.jobs[job.id] = job
                        return job.snapshot()
                if routed is not None:
                    return routed
                if not task:
                    raise ValueError("Create a task and answer its required questions before starting the agent.")
                lifecycle = task.get("lifecycle", {}).get("status", "active")
                if lifecycle not in {"active", "running"}:
                    raise ValueError("Resume this task before sending an agent message.")
                agreement = self.require_success(self.cli(["task", "prompt"]))["output"]
                chat = self.project / ".harness/chat" / task["id"]
                chat.mkdir(parents=True, exist_ok=True)
                previous = read_json(chat / "thread.json", {})
                # The next request can arrive before the persistence worker finishes.
                for previous_job in reversed(list(self.jobs.values())):
                    if not isinstance(previous_job, dict) and previous_job.log_dir.parent == chat:
                        receipt = previous_job.snapshot()
                        if receipt.get('thread_id') and receipt['state'] == 'completed':
                            previous = {'thread_id': receipt['thread_id'], 'task_id': task['id']}
                            break
                if previous.get('task_id') not in {None, task['id']}:
                    raise ValueError('Saved agent thread belongs to a different task.')
                log_dir = chat / uuid.uuid4().hex
                prompt = agreement + "\n\nUse only this project's accepted requirements and instructions. " \
                    "Use the task's configured coding harness. Do not inspect other projects or external evaluation materials. " \
                    "If a required check or permission is unavailable, explain it. Never claim unrun checks passed.\n\nUser message:\n" + message
                agent_config = task.get('agent', {})
                job = self.agent_factory(prompt, self.project, log_dir, executable=self.executable,
                    task_id=task['id'], user_message=message,
                    thread_id=previous.get("thread_id"), model=agent_config.get('model', 'gpt-6.2-astra'),
                    effort=agent_config.get('effort', 'high'),
                    sandbox="read-only" if task["mode"] == "audit" or task.get('answers', {}).get('code_changes') == 'deny' else "workspace-write").start()
                self.jobs[job.id] = job
                def remember():
                    result = job.wait()
                    if result.get("thread_id") and result["state"] == "completed":
                        draft = log_dir / "thread.tmp"
                        draft.write_text(json.dumps({"thread_id": result["thread_id"], "task_id": task["id"]}), encoding="utf-8")
                        draft.replace(chat / "thread.json")
                self.pool.submit(remember)
                return job.snapshot()
            raise ValueError("Unknown endpoint.")


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        content = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def valid_host(self):
        return self.headers.get("Host", "") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {"error": "Loopback host required."})
        url = urlparse(self.path)
        app = self.server.app
        try:
            if url.path == "/api/state":
                return self.respond(200, app.state())
            if url.path == "/api/job":
                query = parse_qs(url.query)
                job = app.jobs.get(query.get("id", [""])[0])
                if not job:
                    raise ValueError("Unknown job.")
                return self.respond(200, app.job_snapshot(job, int(query.get("after", [0])[0])))
            if url.path == '/api/run':
                selected = parse_qs(url.query).get('id', [''])[0]
                if not selected or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in selected):
                    raise ValueError('Select a valid run ID.')
                return self.respond(200, app.cli(['runs', 'show', selected]))
            if url.path == "/api/artifact":
                project = app.require_project()
                relative = parse_qs(url.query).get("path", [""])[0]
                root = (project / ".harness/tasks").resolve()
                target = (root / relative).resolve(strict=True)
                if not target.is_relative_to(root) or target.name not in {"result.json", "stdout.log", "stderr.log", "task.json"} or not target.is_file():
                    raise ValueError("Artifact is outside the allowed task outputs.")
                return self.respond(200, {"path": relative, "content": target.read_text(encoding="utf-8", errors="replace")[:500000]})
            root = ROOT / "web"
            target = (root / (url.path.lstrip("/") or "index.html")).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                return self.respond(404, {"error": "Not found."})
            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except (ValueError, OSError, KeyError) as error:
            self.respond(400, {"error": str(error)})

    def do_POST(self):
        if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.app.token):
            return self.respond(403, {"error": "Invalid local request token."})
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            return self.respond(403, {"error": "Cross-origin request rejected."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1000000:
                raise ValueError("Request size is invalid.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Supply a JSON object.")
            self.respond(200, self.server.app.post(urlparse(self.path).path, data))
        except (ValueError, OSError, KeyError, RuntimeError, TypeError, subprocess.TimeoutExpired) as error:
            self.respond(400, {"error": str(error)})

    def log_message(self, *_):
        pass


class WorkbenchServer(ThreadingHTTPServer):
    def server_close(self):
        super().server_close()
        app = getattr(self, "app", None)
        if app:
            for job in app.jobs.values():
                if not isinstance(job, dict) and job.snapshot()["state"] in {"created", "starting", "running", "cancelling"}:
                    job.cancel()
            app.pool.shutdown(wait=True)


def create_server(project=None, port=8792, executable=None, agent_factory=AgentJob):
    server = WorkbenchServer(("127.0.0.1", port), Handler)
    server.app = Workbench(project, executable, agent_factory)
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--codex")
    options = parser.parse_args()
    server = create_server(options.project, options.port, options.codex)
    print(f"Apex Workbench: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
