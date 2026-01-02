# Concurrency helpers for SafeCmd execution

This ExecPlan is a living document. The sections "Progress",
"Surprises & Discoveries", "Decision Log", and "Outcomes & Retrospective"
must be kept up to date as work proceeds.

No PLANS.md file is present in the repository root, so there is no additional
plan governance to follow beyond this ExecPlan.

## Purpose / big picture

Cuprum needs a helper that runs multiple `SafeCmd` instances concurrently with
an optional concurrency limit, while preserving existing hook behaviour and
returning aggregated results. When this work is complete, users can run a batch
of curated commands (for example, multiple `git status` calls) with a single
API, inspect individual `CommandResult` values, and still rely on before/after
hooks and observe events as if each command were run individually. Success is
observable by new unit tests and pytest-bdd scenarios that pass, updated design
and user documentation, and a roadmap entry marked as done.

## Progress

- [x] (2026-01-02 04:24Z) Read roadmap and project guidance, and drafted plan.
- [ ] Add concurrency helper API, result type, and exports.
- [ ] Add unit tests for concurrency helper behaviour and hook preservation.
- [ ] Add behavioural tests and feature coverage for concurrent execution.
- [ ] Update design docs, users' guide, and roadmap entry for 2.3.1.
- [ ] Run format, lint, typecheck, and test gateways.

## Surprises & discoveries

- None yet.

## Decision log

- Decision: Implement the helper in `cuprum/sh.py` as async
  `run_concurrent(...)` plus sync `run_concurrent_sync(...)`, returning a new
  `ConcurrentResult` dataclass that aggregates per-command outcomes.
  Rationale: Keep the public API close to `SafeCmd.run` and `Pipeline.run`,
  preserve hook semantics by delegating to `SafeCmd.run`, and provide a stable,
  typed aggregate object rather than a loose tuple.
  Date/Author: 2026-01-02 (Codex).

- Decision: Use `asyncio.TaskGroup` plus an `asyncio.Semaphore` to enforce
  `max_concurrency` while keeping results in input order, and capture
  per-command exceptions rather than cancelling the whole batch.
  Rationale: TaskGroup provides structured concurrency and predictable
  cancellation, while the semaphore keeps parallelism bounded without losing
  ordering. Capturing exceptions enables aggregated results even when some
  commands fail early.
  Date/Author: 2026-01-02 (Codex).

## Outcomes & retrospective

- Not started.

## Context and orientation

The primary execution runtime lives in `cuprum/sh.py`, which defines
`SafeCmd.run`, `SafeCmd.run_sync`, `Pipeline.run`, and `Pipeline.run_sync`.
Hooks (before/after and observe events) are implemented in `cuprum/context.py`
and wired into `SafeCmd.run` via `_run_before_hooks` and structured events in
`cuprum/_observability.py`. The design reference for parallel execution is in
`docs/cuprum-design.md` §8.3 (Parallel Execution). User-facing guidance lives
in `docs/users-guide.md`, currently covering pipelines and observability. Unit
tests are colocated under `cuprum/unittests/`, while behavioural tests use
pytest-bdd in `tests/behaviour/` with matching Gherkin scenarios in
`tests/features/`.

Terminology used here:

- "Hook semantics" means the same before/after/observe hooks that fire for a
  single `SafeCmd.run` call must also fire when commands are executed via the
  new helper.
- "Aggregated results" means the helper returns a single object containing a
  per-command outcome in input order, rather than forcing the caller to manage
  `asyncio.gather` manually.

## Plan of work

Add a new `ConcurrentResult` dataclass and concurrency helper functions in
`cuprum/sh.py`. The helpers should accept a sequence of `SafeCmd` instances,
optional `capture`, `echo`, and `ExecutionContext` parameters (applied to each
command), and an optional `max_concurrency` limit. The async helper should
schedule each `cmd.run(...)` inside a semaphore guard and store the outcome in
input order. Use `asyncio.TaskGroup` to manage tasks and to honour structured
cancellation, but catch per-command exceptions inside each task so the helper
can return a full aggregated result. Validate `max_concurrency` to be positive
when provided; raise `ValueError` for invalid values.

Expose the new helper and result type by updating `__all__` in `cuprum/sh.py`
and adding re-exports in `cuprum/__init__.py` (and `cuprum/sh.py` module-level
namespace). Do not bypass `SafeCmd.run`, so hooks and observe events behave
identically to standalone execution.

Add unit tests in `cuprum/unittests/test_concurrency_helpers.py` that cover:
input order preservation, concurrency limit enforcement (using timing with
generous thresholds), hook invocation counts (before/after and observe hooks),
and aggregated exception capture (for example, a command disallowed by the
allowlist should appear as an exception entry without cancelling the batch).
Follow existing patterns in `cuprum/unittests/test_safe_cmd_run.py` for running
async code with `asyncio.run`.

Add behavioural tests in `tests/behaviour/test_concurrency_helpers.py` with a
corresponding Gherkin file `tests/features/concurrency_helpers.feature`. Cover
scenarios for running multiple commands concurrently, limiting concurrency to
one (sequential behaviour), and validating aggregated results in the user
visible API. Keep timings stable by using modest sleep intervals and wide
assertion margins to avoid flakes.

Update documentation:

- `docs/cuprum-design.md` §8.3 should describe the new helper, its signature,
  concurrency limit behaviour, and aggregated results (including how
  exceptions are surfaced).
- `docs/users-guide.md` should include a new "Concurrent execution helpers"
  section with examples for running a batch, applying a concurrency limit, and
  interpreting `ConcurrentResult`.
- `docs/roadmap.md` should mark task 2.3.1 and its documentation subtask as
  done after implementation and documentation updates.

After documentation edits, run `make fmt` to apply formatting, then run
`make markdownlint` and `make nixie` for Markdown validation, along with the
required `make check-fmt`, `make lint`, `make typecheck`, and `make test`.

## Concrete steps

1. Inspect the design and docs locations to anchor edits.

    rg -n "Parallel Execution" docs/cuprum-design.md
    rg -n "Pipeline execution" docs/users-guide.md

2. Implement the new API in `cuprum/sh.py` and update exports in
   `cuprum/__init__.py`.

3. Add unit tests under `cuprum/unittests/test_concurrency_helpers.py` and
   behavioural tests under `tests/behaviour/test_concurrency_helpers.py` plus
   `tests/features/concurrency_helpers.feature`.

4. Update documentation files: `docs/cuprum-design.md`,
   `docs/users-guide.md`, and `docs/roadmap.md` (mark 2.3.1 done).

5. Run formatting, linting, typechecking, and tests with logged output.

    set -o pipefail
    make fmt 2>&1 | tee /tmp/make-fmt.txt

    set -o pipefail
    make markdownlint 2>&1 | tee /tmp/make-markdownlint.txt

    set -o pipefail
    make nixie 2>&1 | tee /tmp/make-nixie.txt

    set -o pipefail
    make check-fmt 2>&1 | tee /tmp/make-check-fmt.txt

    set -o pipefail
    make lint 2>&1 | tee /tmp/make-lint.txt

    set -o pipefail
    make typecheck 2>&1 | tee /tmp/make-typecheck.txt

    set -o pipefail
    make test 2>&1 | tee /tmp/make-test.txt

## Validation and acceptance

- Unit tests: the new `cuprum/unittests/test_concurrency_helpers.py` tests
  fail before the helper exists and pass once implemented.
- Behavioural tests: new pytest-bdd scenarios in
  `tests/features/concurrency_helpers.feature` pass, demonstrating concurrent
  execution, concurrency limiting, and aggregated results.
- Documentation: `docs/cuprum-design.md` and `docs/users-guide.md` describe
  the helper and show examples; `docs/roadmap.md` marks 2.3.1 as done.
- Quality gates: `make check-fmt`, `make lint`, `make typecheck`, and
  `make test` all succeed (with logs preserved in `/tmp` per instructions).

## Idempotence and recovery

All steps are safe to re-run. If a test fails, fix the underlying issue and
re-run the specific `make` target. If documentation formatting fails, run
`make fmt` again and re-run the Markdown checks. If concurrency tests are
flaky due to timing, increase sleep durations or widen timing tolerances in
the tests rather than disabling coverage.

## Artifacts and notes

Capture short excerpts of the `make` command outputs in this section when
executed, for example:

    make test
    ...
    1 failed, 134 passed in 12.34s

Replace the example with real output once the commands are run.

## Interfaces and dependencies

Add the following public API in `cuprum/sh.py` (and re-export in
`cuprum/__init__.py`):

    @dataclass(frozen=True, slots=True)
    class ConcurrentResult:
        results: tuple[CommandResult | Exception, ...]

        @property
        def ok(self) -> bool: ...

        @property
        def errors(self) -> tuple[Exception, ...]: ...

        @property
        def successes(self) -> tuple[CommandResult, ...]: ...

    async def run_concurrent(
        commands: cabc.Sequence[SafeCmd],
        *,
        capture: bool = True,
        echo: bool = False,
        context: ExecutionContext | None = None,
        max_concurrency: int | None = None,
    ) -> ConcurrentResult: ...

    def run_concurrent_sync(
        commands: cabc.Sequence[SafeCmd],
        *,
        capture: bool = True,
        echo: bool = False,
        context: ExecutionContext | None = None,
        max_concurrency: int | None = None,
    ) -> ConcurrentResult: ...

Dependencies stay within the standard library (`asyncio`, `dataclasses`,
`collections.abc`, `typing`) and existing Cuprum modules. Tests use `pytest`
for unit coverage and `pytest-bdd` for behavioural scenarios.
