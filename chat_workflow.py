"""Translate explicit chat choices into the same project CLI commands as the UI."""
from datetime import datetime, timezone
import json
import uuid


MODES = {"audit", "refactor", "feature", "build"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load(path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


class ChatWorkflow:
    def __init__(self, app):
        self.app = app
        self.home = app.require_project() / ".harness"
        self.records = self.home / "chat" / "workflow"
        self.draft = self.records / "intake.json"

    def state(self):
        assignments = load(self.records / "assignments.json", {})
        rows = []
        for path in sorted((self.records / "messages").glob("*.json")):
            row = load(path)
            row["task_id"] = row.get("task_id") or assignments.get(row.get("intake_id"))
            rows.append(row)
        return {"chat_intake": load(self.draft), "workflow_messages": rows}

    def reply(self, message, response, task_id=None, intake_id=None, exit_code=0):
        now = datetime.now(timezone.utc)
        record = {"id": str(uuid.uuid4()), "task_id": task_id, "intake_id": intake_id,
                  "started_at": now.isoformat(), "user_message": message, "response": response, "exit_code": exit_code}
        filename = now.strftime("%Y%m%dT%H%M%S%f") + "-" + record["id"] + ".json"
        write_json(self.records / "messages" / filename, record)
        return {"kind": "workflow", **record}

    @staticmethod
    def question_text(question):
        lines = [question["text"]]
        for option in question.get("option_details", []):
            suggested = " (recommended)" if option["id"] == question.get("suggested_answer") else ""
            lines.append(f"{option['id']}{suggested} — {option['label']}. {option['effect']}")
        return "\n".join(lines)

    def missing(self, task):
        schema = load(self.home / "config" / "questions.json", {})["questions"]
        questions = [q.copy() for q in schema if q["required"] and q["id"] not in task["answers"]]
        for question in questions:
            if "recommended_by_mode" in question:
                question["suggested_answer"] = question["recommended_by_mode"][task["mode"]]
            if task["mode"] == "audit" and question["id"] == "code_changes":
                question["option_details"] = [o for o in question["option_details"] if o["id"] == "deny"]
        return questions

    def route(self, message):
        value = message.strip()
        word, _, argument = value.partition(" ")
        command = word.lower()
        argument = argument.strip()
        if value.lower() in {"help", "/help"}:
            return {"kind": "fixed_help", "response": self.app.cli(["help"])["output"]}
        if not (self.home / "project.json").is_file():
            if value.lower() != "/setup":
                raise ValueError("Install the workbench with /setup, then choose a task mode.")
            self.app.require_success(self.app.cli(["setup", "init"]))
            return self.reply(message, "Workbench installed. Send a mode and action, such as audit Review the order total.")
        task = self.app.active()
        task_id = task["id"] if task else None
        draft = load(self.draft)
        if draft and draft["previous_task"] != task_id:
            self.draft.unlink()
            draft = None
        if command == "/cancel-setup":
            if not draft:
                raise ValueError("There is no pending task setup.")
            self.draft.unlink()
            return self.reply(message, "Task setup cancelled.", task_id, draft["id"])
        direct = {
            "/discover": ["setup", "discover"], "/status": ["task", "status"], "/runs": ["runs", "list"],
            "/verify": ["task", "verify"], "/report": ["task", "report"], "/finish": ["task", "finish"]
        }
        args = direct.get(command)
        if args and argument:
            raise ValueError("This command takes no additional text.")
        if command in {"/pause", "/stop", "/repair"}:
            action = "repair-round" if command == "/repair" else command[1:]
            args = ["task", action, "--reason", argument]
        elif command == "/resume":
            args = ["task", "resume"] + (["--id", argument] if argument else [])
        elif command == "/benchmark" or command == "/check":
            parts = argument.split()
            if len(parts) != (1 if command == "/benchmark" else 2) or parts[-1] not in {"baseline", "after"}:
                raise ValueError("Use /check NAME baseline|after or /benchmark baseline|after.")
            job = self.app.command_job(["benchmark" if command == "/benchmark" else "run", *parts[:-1], "--phase", parts[-1]])
            return self.reply(message, f"Started check job {job['id']}. Inspect its result in the task record.", task_id)
        if args:
            result = self.app.cli(args)
            selected = self.app.active()
            return self.reply(message, result["output"], selected["id"] if selected else task_id,
                              exit_code=result["exit_code"])
        if value.lower() == "/setup":
            return self.reply(message, "The workbench is installed. Send /discover to inspect the project.", task_id)
        if task and task["status"] == "unverified":
            raise ValueError(task["lifecycle"]["reason"])
        if draft:
            if not draft["action"]:
                draft["action"] = value
                write_json(self.draft, draft)
                return self.reply(message, "Which files, components, and behavior are in scope?", intake_id=draft["id"])
            result = self.app.require_success(self.app.cli([draft["mode"], "--action", draft["action"], "--scope", value]))
            created = result["data"]["task_id"]
            assignments = load(self.records / "assignments.json", {})
            assignments[draft["id"]] = created
            write_json(self.records / "assignments.json", assignments)
            self.draft.unlink()
            missing = self.missing(self.app.active())
            return self.reply(message, "Task created.\n" + self.question_text(missing[0]), created, draft["id"])
        mode = command.lstrip("/")
        if mode in MODES and (command.startswith("/") or not task or task["status"] != "active"):
            if task and task["status"] == "active":
                raise ValueError("Finish, pause, or stop the active task before starting another.")
            draft = {"id": str(uuid.uuid4()), "mode": mode, "action": argument, "previous_task": task_id}
            write_json(self.draft, draft)
            prompt = "Which files, components, and behavior are in scope?" if argument else "What action should this task complete?"
            return self.reply(message, prompt, intake_id=draft["id"])
        if not task or task["status"] != "active":
            raise ValueError("Start a task with audit, refactor, feature, or build followed by its action. Use /resume to resume a stopped or paused task.")
        if command == "/restrict":
            question, separator, citation = argument.partition(" ")
            if not separator or question not in {"code_changes", "migrations", "dependencies"}:
                raise ValueError("Use /restrict QUESTION followed by the exact restriction from this task. For a file, use @FILE | QUOTE.")
            if question in task["answers"]:
                return self.reply(message, "This task already records a decision for " + question + ".", task_id)
            source = {"kind": "task_request", "quote": citation}
            if citation.startswith("@"):
                path, separator, quote = citation[1:].partition(" | ")
                if not separator:
                    raise ValueError("Separate the project file and its exact quote with a vertical bar.")
                source = {"kind": "project_file", "path": path, "quote": quote}
            self.app.cli_with_json(["answer", question, "deny", "--answer-channel", "chat", "--id", task_id], source)
            missing = self.missing(self.app.active())
            followup = self.question_text(missing[0]) if missing else "Agreement recorded. Send a message to the coding agent."
            return dict(self.reply(message, "Restriction recorded for this task.\n" + followup, task_id),
                        agreement_complete=not missing)
        missing = self.missing(task)
        if missing:
            question = missing[0]
            options = question.get("option_details", [])
            match = next((row for row in options if value.lower() in {row["id"].lower(), row["label"].lower()}), None)
            if not match:
                return self.reply(message, self.question_text(question), task_id)
            self.app.require_success(self.app.cli(["answer", question["id"], match["id"], "--id", task_id, "--answer-channel", "chat"]))
            text = self.question_text(missing[1]) if len(missing) > 1 else "Agreement recorded. Send a message to the coding agent."
            return dict(self.reply(message, text, task_id), agreement_complete=len(missing) == 1)
        return None
