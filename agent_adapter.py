"""Run an owned Codex CLI turn and expose its real JSONL events."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import uuid

DISABLED_FEATURES = ("hooks", "memories", "plugins")


def subprocess_environment():
    env = os.environ.copy()
    if os.name == "nt":
        # This CLI is a standalone child. It has no desktop app identity.
        for key in ("CODEX_WINDOWS_REGISTERED_CORE", "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY",
                    "CODEX_PERMISSION_PROFILE"):
            env.pop(key, None)
    return env


def feature_flags():
    return [argument for feature in DISABLED_FEATURES for argument in ("--disable", feature)]


def resolve_executable(explicit=None):
    candidate = explicit or os.environ.get("APEX_CODEX_EXECUTABLE") or shutil.which("codex")
    if not candidate:
        raise RuntimeError("Codex CLI was not found. Set APEX_CODEX_EXECUTABLE to its executable path.")
    resolved = Path(candidate).expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError("The configured Codex CLI executable does not exist.")
    if resolved.suffix.lower() in {".cmd", ".bat"}:
        raise RuntimeError("Configure the native Codex executable, not a shell wrapper.")
    return str(resolved)


def capabilities(executable=None, project=None):
    """Inspect local help without launching a model or reading credentials."""
    try:
        exe = resolve_executable(executable)
        def read_help(*args):
            result = subprocess.run([exe, *args], cwd=project, capture_output=True, text=True, timeout=15, check=False)
            if result.returncode:
                raise RuntimeError("Codex capability discovery failed.")
            return result.stdout
        version = read_help("--version").strip()
        execute = read_help("exec", "--help")
        resume = read_help("exec", "resume", "--help")
        settings = {}
        for line in read_help("features", "list", *feature_flags()).splitlines():
            columns = line.split()
            if len(columns) >= 3 and columns[0] in DISABLED_FEATURES:
                settings[columns[0]] = columns[-1]
        project_rules = []
        project_skills = []
        if project:
            root = Path(project)
            project_rules = [name for name in ("AGENTS.md", "AGENTS.override.md") if (root / name).is_file()]
            for location in (".agents/skills", ".codex/skills"):
                project_skills.extend(str(path.relative_to(root)) for path in (root / location).glob("*/SKILL.md"))
        return {"available": True, "executable": exe, "version": version,
                "json_events": "--json" in execute, "resume": "SESSION_ID" in resume,
                "ignore_user_config": "--ignore-user-config" in execute,
                "feature_flags_reported_by_cli": settings, "project_root_instruction_files": project_rules,
                "project_skill_files": project_skills, "skill_execution": "Unverified",
                "interactive_command_approval": False,
                "limitations": ["Uses existing local CLI authentication; this adapter does not manage credentials.",
                                "No interactive command-approval bridge. Denied commands must be reported.",
                                "Host sandbox availability depends on the installed CLI and operating system."]}
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "error": str(error), "interactive_command_approval": False}


def build_command(executable, cwd, thread_id=None, model="gpt-6.2-astra", effort="high", sandbox="workspace-write"):
    root = Path(cwd).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The agent workspace must be a directory.")
    if sandbox not in {"read-only", "workspace-write"}:
        raise ValueError("Only read-only and workspace-write sandboxes are supported.")
    if effort not in {"low", "medium", "high", "xhigh"}:
        raise ValueError("Unsupported reasoning effort.")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("A model name is required.")
    if thread_id:
        uuid.UUID(thread_id)
    args = [resolve_executable(executable), "exec"]
    if thread_id:
        args.append("resume")
    args += ["--json", "--ignore-user-config", "--skip-git-repo-check", "-m", model,
             "-c", f"model_reasoning_effort={json.dumps(effort)}",
             "-c", 'approval_policy="never"', "-c", f"sandbox_mode={json.dumps(sandbox)}",
             *feature_flags(),
             "-c", "memories.use_memories=false", "-c", "memories.generate_memories=false"]
    # Ignoring user configuration also drops the Windows sandbox backend.
    # Select the restricted workspace runner explicitly for new and resumed turns.
    if os.name == "nt":
        args += ["-c", 'windows.sandbox="elevated"']
    # Resume inherits cwd from Popen and accepts the sandbox through -c.
    if not thread_id:
        args += ["--sandbox", sandbox, "-C", str(root)]
    args += [thread_id, "-"] if thread_id else ["-"]
    return args


class AgentJob:
    """One asynchronous model turn. Never searches for or stops unrelated processes."""
    def __init__(self, message, cwd, log_dir, executable=None, thread_id=None,
                 model="gpt-6.2-astra", effort="high", sandbox="workspace-write",
                 task_id=None, user_message=None):
        if not isinstance(message, str) or not message.strip():
            raise ValueError("A nonempty message is required.")
        self.id = str(uuid.uuid4())
        self.cwd = str(Path(cwd).resolve(strict=True))
        self.log_dir = Path(log_dir).resolve()
        self.command = build_command(executable, self.cwd, thread_id, model, effort, sandbox)
        self.message = message
        self.task_id = task_id
        self.user_message = user_message
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.model, self.effort, self.sandbox = model, effort, sandbox
        self.thread_id = thread_id
        self.state = "created"
        self.process = None
        self.response = ""
        self.error = None
        self.exit_code = None
        self.events = []
        self.lock = threading.RLock()
        self.worker = None
        self.cancelled = False
        self.completed_turn = False
        self.command_policy_denials = 0

    def start(self):
        with self.lock:
            if self.state != "created":
                raise RuntimeError("This job has already started.")
            self.log_dir.mkdir(parents=True, exist_ok=False)
            (self.log_dir / "turn.json").write_text(json.dumps({
                "id": self.id, "task_id": self.task_id, "user_message": self.user_message,
                "started_at": self.started_at
            }), encoding="utf-8")
            self.state = "starting"
            self.worker = threading.Thread(target=self._run, daemon=True, name=f"agent-{self.id}")
            self.worker.start()
        return self

    def _event(self, event, log):
        with self.lock:
            self.events.append(event)
            log.write(json.dumps(event, ensure_ascii=False) + "\n")
            log.flush()
            kind = event.get("type")
            if kind == "thread.started":
                received = event.get("thread_id")
                try:
                    uuid.UUID(received)
                except (ValueError, TypeError, AttributeError):
                    self.error = "Codex returned an invalid session identifier."
                    return
                if self.thread_id and received != self.thread_id:
                    self.error = "Codex resumed a different session than requested."
                else:
                    self.thread_id = received
            elif kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                self.response = event["item"].get("text", "")
            elif kind == "turn.completed":
                self.completed_turn = True
            elif kind in {"error", "turn.failed"}:
                detail = event.get("error", event)
                self.error = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            elif kind == "adapter.diagnostic" and "blocked by policy" in event.get("message", "").lower():
                self.command_policy_denials += 1

    def _run(self):
        try:
            flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            with (self.log_dir / "events.jsonl").open("x", encoding="utf-8") as log:
                with self.lock:
                    if self.cancelled:
                        self.state = "cancelled"
                        return
                    self.process = subprocess.Popen(self.command, cwd=self.cwd, env=subprocess_environment(), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **flags)
                    self.state = "running"
                def stderr_reader():
                    for line in self.process.stderr:
                        self._event({"type": "adapter.diagnostic", "message": line.rstrip()}, log)
                reader = threading.Thread(target=stderr_reader, daemon=True)
                reader.start()
                try:
                    self.process.stdin.write(self.message)
                    self.process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                for line in self.process.stdout:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError()
                    except (ValueError, json.JSONDecodeError):
                        event = {"type": "adapter.diagnostic", "message": line.rstrip()}
                    self._event(event, log)
                code = self.process.wait()
                reader.join()
                self.process.stdout.close()
                self.process.stderr.close()
                with self.lock:
                    self.exit_code = code
                    if self.cancelled:
                        self.state = "cancelled"
                    elif code != 0 or self.error or not self.completed_turn or not self.thread_id or not self.response.strip():
                        self.state = "failed"
                        self.error = self.error or f"Codex did not complete the turn (exit {code}). Check CLI authentication and event logs."
                    else:
                        self.state = "completed"
        except Exception as error:
            with self.lock:
                self.error = str(error)
                self.state = "cancelled" if self.cancelled else "failed"
        finally:
            # Persist state, not environment variables or authentication material.
            summary = self.snapshot()
            summary.pop("events", None)
            (self.log_dir / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    def snapshot(self, after=0):
        if not isinstance(after, int) or after < 0:
            raise ValueError("Event cursor must be a nonnegative integer.")
        with self.lock:
            return {"id": self.id, "state": self.state, "pid": self.process.pid if self.process else None,
                    "model": self.model, "effort": self.effort, "sandbox": self.sandbox,
                    "task_id": self.task_id, "user_message": self.user_message, "started_at": self.started_at,
                    "command_policy_denials": self.command_policy_denials,
                    "thread_id": self.thread_id, "response": self.response, "error": self.error,
                    "exit_code": self.exit_code, "events": self.events[after:], "next_cursor": len(self.events)}

    def cancel(self):
        with self.lock:
            if self.state in {"completed", "failed", "cancelled"}:
                return False
            self.cancelled = True
            process = self.process
            if not self.worker and not process:
                self.state = "cancelled"
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        return True

    def wait(self, timeout=None):
        if self.worker:
            self.worker.join(timeout)
        return self.snapshot()
