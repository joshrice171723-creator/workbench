# Agent and chat guide

## Connect the local Codex CLI

The built-in coding agent uses a native Codex executable and the selected project's working directory. Install Ruby 3.1 or later, Python 3.11, and a Codex CLI that supports `exec --json`, `exec resume`, `--ignore-user-config`, and the adapter's feature flags. Sign in through the CLI you intend to run and confirm that your account can use the configured model. A model turn uses that account's access and applicable usage limits.

Start the server from the project with `ruby .harness/bin/harness ui --project .`. If your PATH resolves Codex to a Windows batch wrapper, pass the full path of the native executable. For example, if `Get-Command codex.exe` returns the desired installation, use the following commands.

```powershell
$env:APEX_CODEX_EXECUTABLE = (Get-Command codex.exe).Source
ruby .harness/bin/harness ui --project . --port 8792
```

You can also pass `--codex` with the native executable's path to the UI launcher. Use `HARNESS_PYTHON` to choose the Python executable. **Overview → Check agent availability** inspects local CLI help and feature flags. It lists recognized project rule and skill files. The probe does not start a model, verify login, or demonstrate that the model followed a skill.

## Start a task through chat

Open **Agent chat** or use a second terminal connected to the same server. From the project, send one message per call. Use the address printed by the server when running on a different port.

```powershell
ruby .harness/bin/harness chat --project . --url http://127.0.0.1:8792 --message "/help"
ruby .harness/bin/harness chat --project . --url http://127.0.0.1:8792 --message "audit Review the inventory total"
ruby .harness/bin/harness chat --project . --url http://127.0.0.1:8792 --message "stock.rb and its public callers"
```

On a project without an installation, first send `/setup` from the browser or the package launcher. Use `/discover` and configure at least one meaningful project check in **Project setup** or with the Ruby CLI.

The workbench asks for the action if you supplied only a mode, then the scope. It creates the task when you submit the scope. For each required question, reply with one displayed option ID or label. For example, `deny` records denial for the question currently displayed. The agent cannot infer remaining decisions from this answer. Use `/cancel-setup` to abandon an unfinished intake before task creation. After task creation, use `/stop REASON` to end the task.

Once the agreement is complete, ordinary messages start or continue a real Codex turn. The adapter includes the current task prompt, explicit answers and pinned engine path in its input. Ask a project question, read the reply, then answer in the same chat. Later successful turns resume the saved Codex session for that task. Use a new task for a separate agreement.

While answering task questions, use `/restrict migrations Keep schema fixed.` if the exact sentence appears in this task's action or scope. For a project decision, use `/restrict dependencies @DECISIONS.md | Leave dependencies unchanged.`. These commands record denials and their sources for this task. Chat continues with the next unresolved question.

| Chat input | What it does |
| --- | --- |
| `/help` or `help` | Returns fixed CLI help. |
| `/setup`, `/discover` | Installs the layer or inspects command candidates. |
| `audit ACTION`, `refactor ACTION`, `feature ACTION`, `build ACTION` | Starts intake when there is no active task. |
| `/status`, `/runs` | Shows current task status or lists project runs. |
| `/pause REASON`, `/stop REASON`, `/resume [ID]` | Records a lifecycle change. An omitted ID selects the current task pointer. |
| `/check NAME baseline`, `/check NAME after` | Starts the configured check job. Read its recorded result before relying on it. |
| `/benchmark baseline`, `/benchmark after` | Starts the fixed benchmark for the selected phase. |
| `/repair REASON`, `/verify`, `/report`, `/finish` | Records a repair round, verifies the current snapshot, records a report, or completes a passing task. |

Use **Cancel** in the UI for a live agent job owned by this server. A terminal wait timeout can leave the agent running. Check the job in the UI before sending another request. A project check is a separate subprocess. Consult its task record after execution.

## Agent permissions and records

Audit and tasks with denied code changes request a read-only agent sandbox. An agreed implementation requests workspace-write. The adapter disables hooks, memories and plugins, tells Codex to ignore user configuration, and uses a noninteractive approval policy. Project rules and the pinned coding harness still need to be read by the agent. The local server listens on loopback and requires its request token for mutations. The project and its local records rely on the operating system's file permissions.

The operating system and Codex can still deny file access or command execution. Read the turn's response and events. If the agent could not read the pinned instructions, inspect the source, edit a file, or run a check, record which operation failed. A completed chat turn indicates that a model returned a response. Only actual file changes and check receipts establish implementation and verification. Use a configured project check from the CLI or UI when its command is authorized for your host. Keep the identity of the person or process that performed each action in the report.

On Windows, Workbench starts a standalone Codex child with the task's sandbox settings. It removes inherited Codex Desktop registration markers from the child's environment. The Ruby runtime locates saved task files relative to their containing directory. These are application-level changes. Run Workbench under your normal Windows account with the project's usual permissions.

Use a project folder where your Windows account can read files created by the sandbox. A private temporary folder can restrict access to task records created by the agent. If Workbench cannot read the task lifecycle, it shows **unverified** and the Windows error. You can still inspect readable history. Workbench requires a readable task state before it runs a check, changes the task, or starts another task in that project.

If an older copy reports `registered Core launch requires an installed app`, use the corrected Workbench package. To check the replacement safely, install it in a separate project copy that contains your source and has no existing `.harness` directory. Keep the original project's `.harness` directory with its configuration, checks, transcripts and task history. Setup refuses to overwrite that directory.

The default model and effort are stored in `.harness/project.json` under `agent`. Configure an available model before task creation. Supported effort values are `low`, `medium`, `high` and `xhigh`. Changing the model does not change the selected coding harness.

Workflow messages are under `.harness/chat/workflow/`. Agent turn records are under `.harness/chat/<task-id>/<turn-directory>/`. Inspect `turn.json`, `events.jsonl`, and the final `result.json` for the request, execution and result. A successful turn can save `thread.json` at task level for resumption. After a server restart, a turn without a final result can remain unverified. Project task history and check receipts are under `.harness/tasks/`.

## Use another project agent

A host with project file and terminal access can run the Ruby workflow directly. Add this reference through its normal project instruction mechanism if you want your own agent to use the installed workbench. Keep existing project rules.

```text
Use .harness/docs/WORKFLOW.md. For help, run ruby .harness/bin/harness help.
Ask the user for unresolved task decisions. Record only their explicit answers.
Read the pinned coding harness and the accepted task prompt before dependent work.
Run configured checks in their recorded phases. Report failures and missing evidence.
Record a source-specific review and verify the current snapshot before finishing.
```

This route uses the external host's model, permissions and transcript. The Workbench UI's built-in chat connector launches Codex.
