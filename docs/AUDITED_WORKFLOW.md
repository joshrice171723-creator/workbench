# Codex and Muse workflow

Install Workbench in the project and configure real project checks as in the README. Select the audited workflow **before** starting a new feature or build task. In Project setup, choose **Codex with Muse audits**, enter any Obsidian folders, and save the configuration. The equivalent execution fields in the full configuration are shown below. Omit vaults or use an empty array when starting without notes.

```json
"execution": {
  "workflow": "audited",
  "max_repair_rounds": 3,
  "vaults": ["docs/business", "/path/to/shared/Obsidian"]
}
```

Use a signed-in Codex CLI and Meta Muse Code with headless `exec`, `--prompt-file`, `--output-schema`, and the `:read-only` permission profile. Set `APEX_CODEX_EXECUTABLE` or `APEX_MUSE_EXECUTABLE` to select a local executable. Both providers receive the selected note passages. Choose vault folders containing material appropriate for the task. Paths are relative to the project or absolute. Record measurable targets as assertions in configured project checks. For a new app use required after-only checks for new behavior.

Start the task in Workbench chat and answer the existing task agreement. The last agreement answer starts the coordinator. Chat shows the current stage. Answer any business question there. The final reply includes the builder summary, project check outcomes, final review, and verified source fingerprint. Use `/run` to continue a ready task or retry an interrupted review. An existing agent chat can drive the same coordinator directly:

```sh
ruby .harness/bin/harness loop --project .
ruby .harness/bin/harness loop --project . --answer "Only the staff queue may resolve an ambiguous reply."
ruby .harness/bin/harness loop --project . --status
```

A plan review precedes the baseline and builder. Every completed implementation or repair goes through project checks and a Muse build audit. A passing build audit starts a fresh Muse final audit. Each reviewer can reopen discovery. Both implementation audits can return a finding to the builder. The coordinator records plan versions, questions, answers and stage transitions under `.harness/loops/<id>/`. Audit verdicts and fingerprints are also part of the task ledger and `runs show`. Checks, build review, final review, and the structured quality review must apply to the current source to finish. Each tool runs with the host account's existing credentials. Filesystem-backed records use the local project's trust boundary.

Planning allows three returns. Build reviews use the configured repair limit and final reviews allow three returns. Returning across stages preserves the counters. Resolve a provider problem and resume at the pending stage. At an exhausted limit, the chat reports the remaining finding. Start a new task for an additional budget.

## Ten-minute assessment replay

Use a private evaluation machine with the original Appointment Flow repository (including the two local freeze commits), preinstalled dependencies, disposable test databases and both authenticated CLIs. The public experiment records the outcomes but does not distribute the private comparison fixture. Keep the grader and repaired source outside the candidate directory. The grader command runs from an independently exported app; it must reset its own isolated fixture on each invocation and check **both** an ambiguous delayed YES and a unique eligible YES. Exit 0 on success, 1 on a behavior mismatch and a different nonzero code for setup errors.

Place your settings in `.harness/quick-bench.json` or pass `--fixture PATH`. Supply a required visible project check with its real argument array, and an absolute path to the private independent evaluator. The following is an example configuration:

```json
{
  "assessment_repo": "/assessment/clean-room",
  "checks": [{
    "name": "reply_regressions",
    "argv": ["ruby", "/assessment/visible_reply_checks.rb"],
    "cwd": ".", "required": true, "benchmark": false, "phases": ["after"]
  }],
  "independent_check": ["ruby", "/assessment/private/check_delayed_reply.rb"],
  "answers": {}
}
```

Run the calibration once per fixture revision. It requires the independent evaluator to fail on `9c71028` and pass on `5f2500c`. Then run the quick command repeatedly. Each invocation exports a clean candidate, runs the real providers, and exercises scripted routing cases concurrently. Expected business questions can map to fixed answers in the fixture. Unexpected questions and missing dependencies produce a blocked result. The output names the temporary artifacts directory for inspection or removal.

```sh
ruby bin/harness bench quick --project . --calibrate
ruby bin/harness bench quick --project .
```

The supervisor starts cancelling outstanding process trees at 9 minutes 30 seconds and reserves the remaining time for results. Wall time includes export, installation of Workbench, provider waits, checks and independent grading. Prepare dependencies before starting the clock. A timed-out run counts as a timeout. No full-assessment performance score is inferred from this one behavior.
