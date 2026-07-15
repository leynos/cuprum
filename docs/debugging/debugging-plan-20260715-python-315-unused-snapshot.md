# Debugging Plan: Python 3.15 reports an unused snapshot

- **Generated**: 2026-07-15
- **Issue ID**: PR #153, Actions run 29428346883 job 87396543044
- **Severity**: Medium
- **Falsification sub-agent**: alchemist; Wyvern fallback after the Alchemist
  failed to return a result
- **Planning agent boundary**: This document was prepared by the planning agent.
  Falsification must be executed by the named sub-agent, not by the planning
  agent.

## Problem Statement

The Python 3.15 beta CI job passes all executed tests but exits non-zero because
Syrupy reports one unused snapshot. Python 3.13 does not reproduce the failure.
The expected outcome is for supported interpreter jobs to collect every
snapshot-owning test, or explicitly exclude a snapshot whose owning test cannot
run, without weakening stale-snapshot detection for the rest of the suite.

## Context Summary

| Aspect              | Details                                                          |
| ------------------- | ---------------------------------------------------------------- |
| First observed      | Actions run 29428346883                                          |
| Reproduction rate   | Python 3.15.0b3 CI job                                           |
| Affected components | Pytest collection, Syrupy snapshots, Python 3.15 compatibility   |
| Recent changes      | Canonical stream-drain branch and current CI dependency set      |

### Error Artefacts

```plaintext
collected 635 items / 1 skipped
6 snapshots passed. 1 snapshot unused.
Re-run pytest with --snapshot-update to delete unused snapshots.
make: *** [Makefile:145: test] Error 1
```

### Information Gaps

The abbreviated log does not name the collection-skipped module or the unused
snapshot. The successful comparison run may use a different commit and snapshot
inventory.

---

## Hypotheses

### H1: A snapshot-owning module is skipped during Python 3.15 collection

**Claim**: An import-time compatibility guard skips exactly one module on Python
3.15, preventing its snapshot test from claiming an otherwise valid snapshot.

**Plausibility**: High - the run reports one collection-time skip and one unused
snapshot, while all collected tests pass.

**Prediction**: Collection diagnostics identify a skipped module containing a
snapshot assertion, and removing or narrowing the incompatible import-time
dependency makes the snapshot active.

#### H1 Falsification Plan

| Step | Action | Expected Negative Result |
| ---- | ------ | ------------------------ |
| 1 | Inspect full 3.15 job collection diagnostics and snapshot ownership | The skipped module owns no snapshot |
| 2 | Collect the suspected module under Python 3.15 with skip reasons enabled | It collects normally or skips only individual tests |

**Tooling**: `gh`, pytest collection, source inspection, Python 3.15 if available

**Confidence on falsification**: High; ownership and skip reason directly test
the claim.

---

### H2: Snapshot identity changes under Python 3.15

**Claim**: The owning test runs, but interpreter-specific node identity or
serialization prevents Syrupy from matching one stored snapshot.

**Plausibility**: Medium - alpha/beta interpreter changes can affect collection
or representation, but a mismatch normally reports a failed snapshot rather
than an unused one.

**Prediction**: The owning test is collected under 3.15 but creates a differently
named snapshot when run with `--snapshot-update`.

#### H2 Falsification Plan

| Step | Action | Expected Negative Result |
| ---- | ------ | ------------------------ |
| 1 | Compare collected snapshot test node IDs across 3.13 and 3.15 | Node IDs and snapshot names are identical |

**Tooling**: pytest collection and snapshot file inspection

**Confidence on falsification**: High.

---

### H3: The branch contains a genuinely stale snapshot

**Claim**: One snapshot has no owning test on any interpreter and should be
deleted.

**Plausibility**: Low - the normal local suite and the cited successful CI run
do not report an unused snapshot.

**Prediction**: Snapshot ownership analysis finds no test reference under any
supported interpreter.

#### H3 Falsification Plan

| Step | Action | Expected Negative Result |
| ---- | ------ | ------------------------ |
| 1 | Map every stored snapshot to its test and compare the successful run | The allegedly stale snapshot is claimed outside Python 3.15 |

**Tooling**: snapshot files, test definitions, successful Actions log

**Confidence on falsification**: High.

---

## Recommended Execution Order

1. **H1** - the one-to-one skip/unused correlation is cheapest and strongest.
2. **H3** - ownership mapping distinguishes a stale artefact from a skipped test.
3. **H2** - inspect interpreter-specific identity only if ownership is intact.

## Termination Criteria

- **Root cause identified**: One hypothesis survives direct ownership and
  collection checks while the alternatives are falsified.
- **Escalation trigger**: All snapshot-owning tests collect under 3.15 and no
  snapshot identity difference or stale artefact can be demonstrated.

## Notes for Executing Agent

Use the full failing and successful GitHub Actions logs. Prefer a fix that makes
the owning test collect safely on Python 3.15 or narrowly marks its snapshot as
intentionally excluded. Do not disable Syrupy's unused-snapshot check globally.

## Outcome

H1 survived and H2/H3 were falsified. Syrupy's `--snapshot-details` identified
`cuprum/unittests/__snapshots__/test_maturin_build.ambr` as the unused
collection. The owning test skipped all Python 3.15 interpreters through a
hard-coded version guard. The cited successful job also skipped the test, but
xdist does not aggregate unused-snapshot reports, so that run masked the issue.

The guard was stale: maturin 1.13.3 with PyO3 0.29 successfully built a CPython
3.15 wheel locally. Removing the guard made the serial Python 3.15 unit batch
pass with seven snapshots matched and no unused snapshots. Strict unused
snapshot enforcement remains enabled.
