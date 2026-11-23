# Architectural decision record (ADR) 0030: The pytest session layout is deferred, not consolidated

## Status

Proposed on 2026-11-23. The default suite keeps its current layout: one pytest
process per `PYTEST_TARGETS` entry, serial, as `test-python` has always run it.
An opt-in layout runner and an opt-in session-evidence plugin exist so the
question can be settled from ordinary CI activity. Neither consolidation nor
`pytest-xdist` is enabled by default, and this record names what would change
that.

## Date

2026-11-23.

## Context and problem statement

Issue #607 asks whether `make test-python` pays for process boundaries it does
not need. At the implementation baseline `87de64c2c` the selector expands to 32
entries, which the recipe runs as 31 pytest processes over 681 files. Every
process re-imports the interpreter, `pytest`, its plugins and the `conftest.py`
chain, and re-collects what it will run.

The hypothesis is that this repeated start-up dominates some of the boundaries.
It is measurably non-trivial: a warm `python -c pass` costs 20 ms,
`pytest --version` costs 253 ms, and collecting a single test module costs
about 1.7 s. Three separate behaviour-batch collections cost 8.62 s; one merged
collection of the same 50 files costs 3.72 s. So the fixed per-process cost is
roughly 1.6-1.9 s, and 31 processes spend 50-59 s on it.

The hypothesis is also unproven where it matters. Collection is a small part of
a CI step whose execution phase runs the tests, and two of the batches compile
Rust, where a warm sccache and a cold one differ by minutes. A serial Cargo
constraint does not prove that 31 sequential boundaries are necessary, and
neither does it prove they are dispensable.

Issue #607 additionally forbids buying evidence: no shadow jobs, no extra
matrix legs, no larger runners, and no synthetic runs. Adoption must rest on
matched pairs from ordinary activity, or be deferred.

## Decision drivers

- No new paid CI capacity, and no new cache or artefact charge.
- No change to test coverage, isolation, fixture lifetimes within a batch, or
  failure propagation.
- Every collected node must keep a route into exactly one process.
- Compiler-invoking modules must stay on a fresh process, because their session
  fixtures build artefacts whose purpose is a real build.
- A reader must be able to re-derive every number in a decision from committed
  or attached evidence.

## Considered options

1. **Adopt consolidation now**, on local measurements alone. Rejected: the
   local measurements are collection-only, and local execution of the
   compiler-invoking batches is dominated by compile-admission queueing (one
   module measured 724.5 s wall for 21.3 s CPU while other agents held every
   slot). Adopting on that basis would be a decision from an instrument that
   cannot see the thing it is measuring.
2. **Ship the tooling, keep `baseline` as the default, and defer.** Chosen. The
   default behaviour is byte-for-byte what it was; the candidate can be run on
   the same commit as its control; and the evidence can accumulate from
   ordinary workflow activity without a single extra runner-minute.
3. **Abandon the investigation.** Rejected: the fixed per-process cost is
   measurable and the tooling is small, so the question is worth a cheap answer.

## Decision

The default suite keeps its current layout. Concretely:

- `test-python` keeps its shell loop and is unchanged.
- `PYTEST_LAYOUT` defaults to `baseline`, whose grouping is rendered from the
  existing selectors rather than restated, and whose selection was verified
  identical to the legacy loop: 31 groups, 681 files, same order.
- `test-python-layout` is an opt-in target. Nothing in `make test`, `make lint`
  or any required workflow invokes it.
- `PYTEST_LAYOUT_GROUPS`, which would hold a consolidated grouping, is empty.
  A grouping cannot be enabled by editing a workflow alone.
- The session-evidence plugin is inert unless `CUPRUM_PYTEST_EVIDENCE_DIR` is
  set, and no workflow sets it yet.
- `PYTEST_PARALLEL_TARGETS` is empty, so the parallel phase runs no tests. The
  planner refuses more than two workers and refuses a file claimed by both
  phases.

Stage 2B (selective `pytest-xdist`) is **not attempted**, because Stage 2A has
not established an independent subset. Under the experiment's own gate, that is
the correct outcome rather than a failure.

### Adoption criteria

The layout changes only if all of the following hold, measured from ordinary
workflow activity and analysed with `scripts/pytest_layout_analysis.py`:

- at least five comparable warm pairs per interpreter, paired on interpreter,
  cache-state class, runner label and lockfile and toolchain fingerprints;
- a median paired saving of at least 20 seconds **and** at least 1.5 per cent of
  the control `Run tests` step;
- no pair regressing by more than 15 seconds;
- no unexplained change in collected node IDs, parametrizations, skips or xfails
  between the layouts;
- non-positive incremental paid runner-seconds, and no new cache or artefact
  charge.

Failing any of these, the arm is rejected or reported `inconclusive`, and this
record is amended rather than quietly overturned. A fresh sample is a new
experiment, not a revision of this one.

## Consequences

- **Positive.** The default suite is untouched, so no contributor pays for an
  unproven change. The measurement apparatus is small, unit-tested, and opt-in.
  The question can be answered later from data the project is already producing.
- **Negative.** The repository carries an unused-by-default target, runner and
  plugin until the question is settled. That is deliberate complexity with a
  named owner and a review date, not an abandoned experiment.
- **Negative.** The evidence is not yet collected. This record defers a decision
  rather than making one, and the issue remains open until it is amended.

## Rollback

Rollback is one variable: `PYTEST_LAYOUT=baseline` is already the default, and
`test-python` never consults it. Removing the apparatus entirely means deleting
`scripts/pytest_batches.py`, `scripts/pytest_layout.py`,
`scripts/pytest_phases.py`, `scripts/pytest_child_process.py`,
`scripts/pytest_run_evidence.py`, `scripts/pytest_layout_evidence.py`,
`scripts/pytest_layout_analysis.py`, the `test-python-layout` target, the
`PYTEST_LAYOUT*` variables, and the plugin registration in `conftest.py`. No
production code, test assertion, fixture or selector changes, so nothing else
has to move.

## Owner and review

Owner: the repository maintainer. Review date: 2027-02-28, or the first
ordinary default-branch run after either arm is enabled, whichever comes first.
At review, either the criteria above are met and this record is superseded by
an adoption ADR, or the apparatus is removed and this record is marked Rejected.

## References

- Issue #607, "Experiment: reduce pytest session churn and qualify selective
  xdist with CI evidence".
- [ExecPlan](../execplans/pytest-session-churn-and-selective-xdist.md).
- Issue #499 (default-suite collection gaps), issue #452 (a compiler-heavy test
  and its local timeout), issue #258 (native-boundary execution), issue #606
  (evidence format). These are related, not replaced.
- `pytest-xdist` documentation on session-scoped fixtures, which records that
  workers each collect and may each execute session fixtures. A session fixture
  is not automatically one-per-job.
