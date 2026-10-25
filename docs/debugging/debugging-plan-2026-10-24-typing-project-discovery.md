# Diagnose typing-contract project discovery

Generated: 2026-10-24. Scope: PR #619 post-rebase validation. Falsification
sub-agent: alchemist. The planning agent prepares this plan; the alchemist
executes the experiment after the current gate has stopped.

## Problem statement

The positive fixture and nine negative cases in
`cuprum/unittests/test_sh_typing_contract.py` exceed the 30-second pytest
timeout. The test helper runs `ty check` without a path from a temporary
directory. An unrelated `/tmp/pyproject.toml` defines a buildlimits project.
One checker process used about 6.7 GB of memory and eight CPU cores for a tiny
fixture. The rebase changed no Python source.

The failing gate log is `/tmp/pr619-rebase-test.out`. Native build fixtures
earlier in the run passed after compiler admission waits. Their delays do not
explain the typing failures.

## Hypothesis and falsification

H1: implicit project discovery selects the ancestor project and checks files
outside the fixture directory. This is more plausible than expensive typing of
a single small snippet, but the selected root has not yet been observed.

Prediction: verbose checker output identifies `/tmp` as the project root;
passing the fixture directory explicitly restricts checking and returns the
expected diagnostic within the existing timeout.

After the gate exits, create one owned temporary directory containing a single
invalid builder call copied from the existing test. Invoke the pinned
virtual-environment checker with the test's interpreter and options, first
without a path and then with the owned directory as an explicit path. Capture
verbose output, elapsed time, return code, and diagnostics. Bound each child to
30 seconds and reap only children launched by this experiment. Do not modify
the ancestor configuration or run a repository gate.

H1 is falsified if the implicit run selects only the fixture and completes with
the expected diagnostic, or if an explicit path still scans unrelated files. A
timeout without root or file-scope evidence is inconclusive. A fast explicit
run alone does not prove the cause; report the discovery evidence.

## Termination and follow-up

Return a falsified, not-falsified, or inconclusive verdict with logs. Stop
after this paired experiment. If H1 survives, the implementation decision must
also address inherited checker configuration, rather than merely reducing the
file list. Add a focused regression with a hostile ancestor project before
changing the helper. Do not raise timeouts or suppress diagnostics to pass. If
falsified, revise the hypothesis before further experiments.

No parity implementation is authorized by this investigation. The 6.1.2 plan
remains subject to approval.

## Experiment result

H1 was not falsified. The alchemist observed `/tmp` as the discovered project
and thousands of unrelated files in verbose output. The implicit check timed
out at 30 seconds (31.420 seconds including reaping). The explicit fixture path
indexed one file and reported `invalid-argument-type` in 1.118 seconds. It
still inherited the ancestor configuration, confirming that file scope alone is
insufficient isolation.

Evidence is in `/tmp/pr619-ty-h1-implicit.out`,
`/tmp/pr619-ty-h1-explicit.out`, and `/tmp/pr619-ty-h1-results.json`. Two
hostile-ancestor regression cases failed before the helper change in 0.65
seconds: the checker included an unrelated sibling file and inherited a rule
suppressing invalid argument diagnostics. The fix supplies an explicit fixture
path, project directory, and empty checker configuration. The original
30-second timeout and all expected diagnostics remain unchanged. After the fix,
all 13 typing-contract tests passed in 1.36 seconds, including both regression
cases (`/tmp/pr619-typing-isolation-green.out`). Full post-rebase gates are
recorded in the [ExecPlan](../execplans/6-1-2-boundary-proptest-parity.md).

The full rerun passed the typing regressions and the 4,353-item unit group,
then found the same configuration-discovery risk in the Ruff import-order
contract test. Its checker correctly rejected the fixture, but the assertion
expected `I001` while preview mode emitted `unsorted-imports`. The test now
passes the repository's configuration explicitly and requires its named
diagnostic. The failure is preserved in `/tmp/pr619-rebase-fixed-test.out`.
