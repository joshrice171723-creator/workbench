# Task workflow

Run the commands below from the project directory after `setup init`. Define `harness` in your PowerShell session.

```powershell
function harness { ruby .harness/bin/harness @args }
```

If you use an existing Codex or RubyMine Air session, the agent runs the commands and asks you for missing decisions. If you use the terminal directly, run the commands in order as you complete each stage. Keep configuration and review drafts under `.harness` so that editing a draft does not change the application snapshot.

## Configure a check

In **Project setup**, the guided fields accept a program and one literal argument per line. For example, enter `ruby` as the program and `test_stock.rb` as the argument if that script verifies your project. Set the working directory to `.` for the project root. Choose **Save configuration**. Use **Full configuration JSON** for advanced settings, including benchmark contracts and after-only checks.

The installed engineering instructions are already pinned. To select another engine, enter its instruction file, its version and any additional instruction files. **Read instruction hashes** checks the files and prepares a pin. **Save configuration** applies it. **Check installed adapter** checks that the built-in chat route returns the installed CLI help and that the configured engine files match the pin. A subsequent model turn can verify that the agent actually reads them.

Inspect the accepted requirements, project instructions, existing checks, and source that the request affects. Trace a caller through the owner of its rules, its writes, and its visible result. Run `setup discover --project .` to find command candidates. Inspect what each command executes and which files or services it needs. A configured check executes under the account running Ruby. Chat sandbox restrictions do not apply to that process.

Make a draft of the installed configuration. Keep the installed engine settings unless you intend to change them.

```powershell
Copy-Item .harness/project.json .harness/proposed.json
notepad .harness/proposed.json
```

For a project that already has a Ruby check at `test/stock_test.rb`, its `checks` array could contain this entry. Substitute a real command and a meaningful assertion from your project.

```json
[
  {
    "name": "stock_behavior",
    "argv": ["ruby", "test/stock_test.rb"],
    "cwd": ".",
    "required": true,
    "benchmark": false
  }
]
```

Use an argument array and a working directory inside the project. Install the required project runtime and dependencies through the project's normal process. Apply the complete draft and inspect the returned checks.

```powershell
ruby .harness/bin/harness setup configure --project . --file .harness/proposed.json
```

At least one required behavior or verification check must pass for a task to finish. Checks normally require `baseline` and `after` results. For a new capability in `feature` or `build`, set `"phases": ["after"]` on a check that only makes sense after implementation. Keep baseline coverage for existing behavior. Every benchmark needs both phases. Define workload, inputs, expected results, metrics, check names and workload script paths before running it. [Project benchmarks](BENCHMARKS.md)

## Use your existing coding harness

The installed engine contains an entry document and a manifest of supporting files. Each task records the engine identity, model, reasoning effort and repair limit at creation. Select the engine **before starting the task**.

For an existing single-file harness, store the actual instructions in the project, for example `engineering/ENGINE.md`. From the project root, use the following Ruby through PowerShell to prepare a config draft. This preserves your configured checks and model. It reads the exact file bytes for the SHA-256 pin.

```powershell
@'
require "json"
require "digest"
entry = "engineering/ENGINE.md"
config = JSON.parse(File.read(".harness/project.json"))
config["engine"] = {
  "version" => "engineering-v1",
  "instructions_path" => entry,
  "sha256" => Digest::SHA256.file(entry).hexdigest
}
File.write(".harness/proposed.json", JSON.pretty_generate(config))
'@ | ruby -
ruby .harness/bin/harness setup configure --project . --file .harness/proposed.json
```

For a multi-file harness, insert the following **before** `File.write` in the example. List the actual entry document and each supporting instruction file. The manifest hash covers the sorted names and file hashes in compact JSON.

```ruby
files = [entry, "engineering/REVIEW.md"].sort.to_h do |path|
  [path, Digest::SHA256.file(path).hexdigest]
end
config["engine"]["included_files"] = files
config["engine"]["included_files_sha256"] = Digest::SHA256.hexdigest(JSON.generate(files))
```

Use **Full configuration JSON** in Project setup if you prefer to paste the prepared settings. Confirm the version, path and hash in the configure result. Run `task prompt` after answering the task questions to see which entry path the agent is instructed to read. A changed or missing pinned file blocks that agreement. For a new engine revision, finish or stop the old task, configure the new pin, and start a new task. Resuming older work requires its recorded settings.

## Agree on the task

1. State the action and scope. Resolve conflicts with project instructions or accepted decisions before dependent work. Preserve unrelated changes in the starting directory. A build needs its own product requirements.
2. Start from **New task**, **Agent chat**, or the terminal. The terminal example is an audit of an existing method.
3. Read the listed questions and option effects. Record the user's answer for each required question. Use `notes` for context. Only an explicit valid option records a permission. The program records supplied answers and cannot authenticate who gave them.
4. Read the generated agreement. A schema change requires both the applicable code and migration permissions and ownership of the affected schema. A dependency change must be necessary to the agreed work. A separate external contract requires its own authorization.

Start the audit and list its questions.

```powershell
harness audit --project . --action "Review the inventory total" --scope "stock.rb and its callers"
harness internal questions --project .
```

Record each answer after the user decides. This example confirms the scope and harness. It denies code, schema, and dependency changes.

```powershell
harness internal answer scope_confirmed confirmed --project .
harness internal answer code_changes deny --project .
harness internal answer migrations deny --project .
harness internal answer dependencies deny --project .
harness internal answer engine_integration confirmed --project .
harness task prompt --project .
```

`task prompt` prints the instructions for the agent. Use those instructions for the agreed work.

The five required IDs are `scope_confirmed`, `code_changes`, `migrations`, `dependencies`, and `engine_integration`. Confirm scope and engine integration explicitly. Choose `deny` or `allow_scoped` for code changes and `deny` or `allow` for migrations and dependencies. Audit requires `code_changes=deny`. Each new task starts with fresh answers. A CLI caller can also pass `--file` to a mode with a JSON object of explicit user decisions keyed by these IDs.

To use a restriction that already applies, open **Use a restriction already stated for this task** in the task form. Select the restriction and cite its exact text from this task's action, scope, or a project Markdown or text file. The program checks the quote and stores its source. For a file, it also records the path and SHA-256. A citation can deny a change to code, schema or dependencies. Give any permission to make changes through an explicit answer for this task. The form and chat ask only the remaining questions. The CLI lists them under `unanswered_questions`. Suggestions never select an answer.

A CLI decision file can carry the same sources. This example requires the cited sentence in the current action or scope and the quoted dependency restriction in the named file.

```json
{
  "migrations": {"option": "deny", "source": {"kind": "task_request", "quote": "Keep schema fixed."}},
  "dependencies": {"option": "deny", "source": {"kind": "project_file", "path": "DECISIONS.md", "quote": "Leave dependencies unchanged."}}
}
```

The task records CLI, form, and chat answers with their entry channel. The local runtime records what its caller supplies. The person reviewing the agreement determines whether a cited sentence supports its use.

## Run, review and finish

Run the baseline check before the work. This example uses `stock_behavior` from the configuration above. Replace that name with your configured check.

```powershell
harness internal run stock_behavior --phase baseline --project .
```

Perform the audit or the agreed changes. Then run the same check on the final source and read the task status.

```powershell
harness internal run stock_behavior --phase after --project .
harness task status --project .
```

For a configured benchmark, substitute `internal benchmark --phase baseline` and `internal benchmark --phase after` with the same `--project .`. Record failed attempts. The first executed check freezes the execution configuration and permission choices. A passing baseline is required for the after phase of a two-phase check. Correct a failed prerequisite or report the outstanding issue. A later source change invalidates after evidence for the old snapshot.

The agent reviews the final files and check results. It can copy the template below to start its review. It records the current `snapshot_sha256` from status, the verdict, and evidence for each criterion. For a criterion marked `not_applicable`, it gives the reason and evidence. It records its review as `author: agent`.

```powershell
Copy-Item .harness/templates/REVIEW.json .harness/review.json
```

Confirm a finding against the source before repairing it. Record each repair with `task repair-round --project . --reason "Specific finding"` before editing. The default limit is three rounds. The last allowed round can still run verification and finish. Requesting a further round stops the task.

Submit the completed review, verify the current source, and generate the report. Run `task finish` after verification passes.

```powershell
harness internal review --project . --file .harness/review.json
harness task verify --project .
harness task report --project .
harness task finish --project .
```

Completion requires current passing checks and a structured agent review. The runtime validates hashes, evidence shape and configured gates. The reviewer remains responsible for the accuracy of its evidence. Report the action, constraints, verification and remaining issues. Identify who performed any independent review or human acceptance.

## Pause and inspect history

Pause or stop an active task with `task pause` or `task stop`, `--project .` and `--reason "Reason"`. Resume a manually paused or stopped run with `task resume --project . --id ID` while no other task is active. A completed task stays in history. Start a new task for additional work or when the runtime stopped the previous task for exceeding its repair budget. The UI also has **Task runs** to open records and compare two selected runs.

```powershell
ruby .harness/bin/harness runs list --project .
ruby .harness/bin/harness runs show FIRST_ID --project .
ruby .harness/bin/harness runs compare FIRST_ID SECOND_ID --project .
```

Substitute IDs from the list. Records live under `.harness/tasks/<id>/`. Each check attempt has `result.json`, `stdout.log` and `stderr.log` under `results/<check>/<phase>/<attempt>/`. Inspect command arguments, working directory, exit code, duration, source fingerprints and output. Compare measurements only when the contracts and workloads agree. Keep the project directory and its `.harness` records together when you need its history.
