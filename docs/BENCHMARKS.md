# Create a project benchmark

The benchmark belongs to the host project. The orchestrator supplies a process and a template. It does not supply project-specific operations or expected answers.

## Discover the work

Read the accepted requirements and trace important operations through their normal callers. Select work by frequency, consequence, and expected data growth. Find the rule owners, external reads, local writes, and caller-visible results.

Ask about missing workload expectations. Give clear options and their effects. Explain measurement assumptions. Do not present an invented production workload as a known requirement.

## Define a repeatable contract

For each operation, record the caller, fixture construction, starting state, expected results, workload sizes, metrics, assertions, repetitions, and reset procedure. Reference the check commands and all workload-defining scripts. Keep synthetic data and controlled external services separate from real integrations.

Measure correctness with cost. Check applicable identities, responses, ordering, permission changes, side effects, failures, and retry behavior. The baseline shows current behavior. It does not prove that behavior satisfies accepted policy. Report a conflict instead of silently fixing it during a refactor.

Measure complete operations. Keep database creation and migration time separate. Include required first population, refresh, derived-data maintenance, writes, and retries in their proper stages. Test added unrelated history separately from additional required work.

Use relevant metrics. Candidates include statements, returned rows, model objects, affected rows, payload bytes, external calls, elapsed time, and memory. A small response or low query count can hide a large scan. Record unavailable telemetry. Use repeated timing samples and describe shared-machine noise.

## Freeze before changing application code

Configure the benchmark and its script files before baseline measurement. Run baseline measurement on unchanged application inputs. Lock the contract and workload scripts. Record their hashes and the application fingerprint.

Run the same benchmark after the refactor. Do not weaken assertions, alter data sizes, or provide unmeasured derived values to help the candidate. Include the real production maintenance path.

If the benchmark has a defect, preserve the failed run. Create a new task or benchmark revision with the correction. Rerun both original and candidate using the corrected contract. Do not compare results from different contracts as a matched pair.

## Report evidence

Show actual before-and-after values alongside exact outcomes and input sizes. Explain what each metric measures and which operation produced it. Distinguish observations from source-based predictions. Avoid a composite score unless a fixed and justified method exists before the comparison.

The agent can miss the same issue in code and in its benchmark. A project-authored benchmark supports the internal loop. Separate final evaluation provides a different source of evidence and stays outside the builder's context.
