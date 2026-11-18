# Apex Workbench 0.2.1 validation

The Windows release passed 44 Ruby tests with 511 assertions and 20 Python tests. Edge passed ten first-use checks. A live Codex task recorded a failed check, one repair, two passing rechecks, and a completed task. The host verified the saved evidence.

The bundled engineering instructions retain their own version, generic-2.10.6.3.1-derived-0.2.0.

## Program checks

Run these commands from the extracted directory.

```powershell
ruby -Itest -r./test/cli_regression_test -r./test/workbench_test -e "nil" -- --verbose
python -m unittest -v test_agent_adapter test_server
```

## First-use checks

Use a fresh synthetic project on Windows. Configure a required project check with the guided fields. Select and pin project-specific engineering instructions. Check the installed adapter and compare its help, version and hashes with the installed files.

Create a task that cites a restriction in the current request and a restriction in a project decision file. Confirm that only unresolved questions require answers. Run the configured command. Inspect its output and the answer sources in the run record. Start another task and confirm that it has fresh decisions.

A verified adapter route shows that the installed chat workflow returns the installed help and that selected instruction hashes match. Use a real model turn to check instruction access. Check application behavior and any source changes separately.

The candidate's external receipts record the Windows environment, package hash, test results and browser observations. macOS, Linux, formal ASD-STE100 compliance and human acceptance remain unverified.
