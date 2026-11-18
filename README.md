# Apex Workbench

Apex Workbench coordinates coding tasks from a terminal or a Codex chat. It records the task scope, project rules, user decisions, checks, reviews, and repair rounds in the project.

Use the bundled engineering instructions or pin a different coding harness. The harness defines how the agent should work. Workbench records which instructions apply and checks the evidence required to finish.

- **CLI** to configure a project, start tasks, run checks, and compare past runs.
- **Chat** to agree on a task, answer questions, and work with Codex in the same session.

## How it works

```mermaid
flowchart LR
    CLI["Terminal commands"] --> Workflow["Ruby task workflow"]
    Chat["Terminal chat"] --> Server["Local Python server"]
    Server --> Workflow
    Server --> Codex["Codex CLI"]
    Workflow --> Records["Project .harness records"]
    Engine["Pinned engineering instructions"] --> Agreement["Task agreement"]
    Workflow --> Agreement
    Agreement --> Codex
    Checks["Project checks and benchmarks"] --> Records
```

| Part | Responsibility |
| --- | --- |
| [CLI entry point](bin/harness) | Starts the Ruby workflow and dispatches setup, task, and history commands. |
| [Task workflow](lib/workbench.rb) | Records decisions, validates the pinned instructions, and checks current evidence. |
| [Chat intake](chat_workflow.py) | Routes chat commands and collects decisions before Codex starts. |
| [Codex adapter](agent_adapter.py) | Starts or resumes the selected model session with its task scope. |
| [Engineering instructions](engine/ENGINE.md) | Define the coding, performance, and review rules used by the agent. |
| [Project benchmark guide](docs/BENCHMARKS.md) | Explains how to compare the same workload before and after a change. |

An installed project stores its task records under `.harness/tasks/<id>/`. Each check attempt keeps its result, standard output, and error output.

```text
project/
└── .harness/
    ├── bin/harness
    ├── project.json
    ├── engine/
    └── tasks/
        └── <id>/
            └── results/
                └── <check>/<phase>/<attempt>/
                    ├── result.json
                    ├── stdout.log
                    └── stderr.log
```

## Install in a project

The Windows release was tested with Ruby 3.4.10 and Python 3.11.15. Use Ruby 3.1 or later. Chat also needs Python and a signed-in Codex CLI. Use the CLI workflow without a chat server to run checks and review records.

From the Workbench directory, set the path to an existing project.

```powershell
$project = (Resolve-Path 'C:\src\inventory').Path
ruby bin/harness setup init --project "$project"
Set-Location -LiteralPath $project
ruby .harness/bin/harness setup discover --project .
ruby .harness/bin/harness help
```

The installer preserves existing project instructions. Review the commands found by discovery. Configure a real project check before starting work.

```powershell
Copy-Item .harness/project.json .harness/proposed.json
notepad .harness/proposed.json
```

Set the `checks` entry to use the actual command for your project. This example runs a Ruby test from the project root.

```json
"checks": [
  {
    "name": "stock_behavior",
    "argv": ["ruby", "test/stock_test.rb"],
    "cwd": ".",
    "required": true,
    "benchmark": false
  }
]
```

```powershell
ruby .harness/bin/harness setup configure --project . --file .harness/proposed.json
ruby .harness/bin/harness setup check-engine --project .
```

## Use chat

Start the local server in one terminal. The command is named `ui`. The server also handles terminal chat, so a browser is optional.

```powershell
ruby .harness/bin/harness ui --project . --port 8792
```

In a second terminal, open the project directory. Send a task mode and action.

```powershell
ruby .harness/bin/harness chat --project . --message "/help"
ruby .harness/bin/harness chat --project . --message "refactor Simplify the inventory total"
```

Send the affected scope when the intake asks for it. Reply with an option shown in each question. Every task has its own permissions. Suggested answers grant no permission. An existing restriction can carry over when you cite its exact text and source.

After the agreement is complete, ordinary messages go to the configured Codex agent. The installed default is Astra 6.2 High. Use the same chat endpoint to continue the task.

| Chat command | Use it to |
| --- | --- |
| `/status` or `/runs` | Read the active task or list past task IDs. |
| `/check stock_behavior baseline` | Run the configured check before editing. |
| `/benchmark baseline` | Run the configured benchmark before editing. |
| `/check stock_behavior after` | Repeat the check on the current source. |
| `/repair REASON` | Record a verified finding before a repair. |
| `/verify`, `/report`, or `/finish` | Verify evidence, read the report, or finish the task. |
| `/pause REASON`, `/resume ID`, or `/stop REASON` | Control the current task. |

Pass each command as a `--message`. Replace `stock_behavior` with your check name. Use `/benchmark after` to repeat the same benchmark after a change. [Chat setup and model access](docs/AGENT_ADAPTER.md)

## Use the CLI directly

Choose the mode that matches the request.

| Mode | Task boundary |
| --- | --- |
| `audit` | Inspect the agreed code and report findings. |
| `refactor` | Change the implementation and preserve caller-visible behavior. |
| `feature` | Implement the agreed change in behavior. |
| `build` | Build from the requirements you provide. |

For a read-only audit, start the task and inspect the questions.

```powershell
ruby .harness/bin/harness audit --project . --action "Review the inventory total" --scope "stock.rb and its callers"
ruby .harness/bin/harness internal questions --project .
ruby .harness/bin/harness internal answer scope_confirmed confirmed --project .
ruby .harness/bin/harness internal answer code_changes deny --project .
ruby .harness/bin/harness internal answer migrations deny --project .
ruby .harness/bin/harness internal answer dependencies deny --project .
ruby .harness/bin/harness internal answer engine_integration confirmed --project .
ruby .harness/bin/harness task prompt --project .
```

Confirm the engine choice after you check the configured instructions. Run the baseline and final checks. Record a review tied to the current source.

```powershell
ruby .harness/bin/harness internal run stock_behavior --phase baseline --project .
ruby .harness/bin/harness internal run stock_behavior --phase after --project .
Copy-Item .harness/templates/REVIEW.json .harness/review.json
notepad .harness/review.json
ruby .harness/bin/harness internal review --project . --file .harness/review.json
ruby .harness/bin/harness task verify --project .
ruby .harness/bin/harness task report --project .
ruby .harness/bin/harness task finish --project .
```

Enter the current source fingerprint in the review. Give each criterion a verdict and evidence. The workflow guide also explains how to pin your own harness and configure a benchmark. [Task workflow](docs/WORKFLOW.md)

## Read evidence from past runs

```powershell
ruby .harness/bin/harness runs list --project .
ruby .harness/bin/harness runs show FIRST_ID --project .
ruby .harness/bin/harness runs compare FIRST_ID SECOND_ID --project .
```

Compare runs with the same workload and checks. A source change invalidates earlier final evidence for that task. A failed required check keeps the task incomplete.

The Windows 0.2.1 release passed 44 Ruby tests and 20 Python tests. A live Codex agent recorded one finding, repaired the code, and repeated the checks. The host reopened the review, checks, and completion record from the extracted installation. [Validation record](VALIDATION.md)
