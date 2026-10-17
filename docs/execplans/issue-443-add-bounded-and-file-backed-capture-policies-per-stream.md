# Add bounded and file-backed capture policies per stream

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: IN PROGRESS

## Purpose / big picture

Cuprum's `RunOutputOptions` today exposes exactly one capture switch,
`capture: bool`. Turning it on retains *every* byte a child writes in a Python
`bytearray` on the parent heap; turning it off discards the stream entirely.
`max_echo_line_bytes` looks like a bound but is not one: it caps only the
mirrored copy sent to a presentation sink, and the captured copy stays
complete. There is no third option.

That forces a bad choice on anyone driving a long-running, noisy child — a
build tool, a compiler, a test runner that emits megabytes of progress. Capture
it, and the parent's resident memory tracks the child's total output for the
life of the run. Do not capture it, and when the run fails there is nothing to
report: no trailing diagnostic, no last lines of context, no artefact to attach
to a bug report. Capture is also joint: stdout and stderr cannot be configured
independently, so a caller who wants stderr's last 64 KiB *and* stdout streamed
to a file must currently take both in full.

After this change a caller can do all of the following:

1. Keep today's behaviour by saying nothing: `capture=True` still means "both
   streams captured in full, on the heap", byte for byte, with `stdout` and
   `stderr` delivered as `str`.
2. Bound a stream to its most recent N bytes — a tail — and read back exactly
   how many bytes were dropped, on which stream, with the retained text never
   ending in a half-decoded multi-byte code point.
3. Spill a stream to a temporary file once it exceeds a threshold, reading
   after the run from a path the result reports, with the file owned and
   deterministically removed by the library unless the caller asked to keep it.
4. Configure stdout and stderr independently, to different policies, in the
   same way `echo_stdout` and `echo_stderr` already override `echo`.
5. Observe truncation and spill through an opt-in, payload-free event channel,
   and read per-stream storage metadata from a new `captures` field.
6. Do all of that *without* the bound reaching any downstream pipeline stage —
   a bounded or spilled stage still forwards its complete stream to the next
   stage, byte for byte.

The observable shape of the change, in a REPL with the package importable:

```python
>>> from cuprum import BoundedCapture, FileBackedCapture, RunOutputOptions
>>> options = RunOutputOptions(
...     capture_stdout=BoundedCapture(max_bytes=8),
...     capture_stderr=FileBackedCapture(spill_threshold_bytes=1024),
... )
>>> options.resolved_capture
(BoundedCapture(max_bytes=8), FileBackedCapture(spill_threshold_bytes=1024, directory=None, keep_file=False))
```

and, for a run whose stdout exceeds the bound:

```python
>>> result.stdout
'efghijkl'
>>> result.captures[0].dropped_bytes
8
>>> result.captures[0].total_bytes
16
```

## Constraints

Hard invariants that must hold throughout implementation. Violation requires
escalation, not a workaround.

- **Default behaviour must not change.** With no new field supplied,
  `capture=True` must resolve both streams to `FullCapture()`,
  `CommandResult.stdout` must remain `str | None` holding the complete text,
  and every existing test must pass unmodified except where it pins a shape
  this change deliberately extends.
- **The relay pump must not be touched.** Bytes forwarded from one pipeline
  stage to the next are the child's, unbounded and undecoded. Bounded and
  file-backed capture are properties of the *capture tap* only. This is the
  central acceptance criterion of issue #443 and is the reason the accumulator
  is driven from `_deliver_chunk`'s existing capture step rather than anywhere
  near the relay.
- **`stdout`/`stderr` stay `str | None`.** File-backed capture reports `None`
  there and puts the path in `captures`; the public type contract is not
  widened to a union with a handle type.
- **Byte-exact mode reports no capture metadata.** `BytesCommandResult` has no
  `captures` field, so a `run_bytes()` run cannot report a spill or a
  truncation; the bytes are all it says. The policies still apply there, and a
  spilled stream is read back so the caller receives the child's bytes in full,
  but that read is O(stream size) and gives byte mode none of the
  bounded-memory guarantee that text mode gets.
- **No new runtime dependency.** The spill file uses the standard library's
  `tempfile`; nothing is added to `pyproject.toml`.
- **No public API removal.** `RunOutputOptions.capture`, `.echo`,
  `.echo_stdout`, `.echo_stderr`, `.max_echo_line_bytes`, `CommandResult`'s
  seven positional slots, and `RunOutputOptions`'s first six positional slots
  all keep their meaning.
- **No file may exceed 400 lines**, per the repository's code-style rules.
  Several of the modules this change extends are already close to that; new
  behaviour goes into new modules rather than growing an existing one past the
  ceiling.
- **en-GB-oxendict spelling** everywhere in identifiers, comments, docstrings,
  and prose; the `make spelling` gate enforces it over every tracked file.
- Every source module begins with a `//!`-equivalent module docstring
  (Python: a module-level `"""..."""`), and every public API carries a
  comprehensive NumPy-style docstring.

## Tolerances (exception triggers)

Thresholds that trigger escalation when breached.

- Scope: if implementation requires modifying more than 30 tracked files or
  2500 net lines, stop and escalate.
- Interface: if a *released* public signature must change in a way that is not
  purely additive, stop and escalate. Adding defaulted fields and new types is
  in scope; reordering or removing existing ones is not.
- Dependencies: if any new third-party runtime dependency is required, stop and
  escalate.
- Iterations: if a single test still fails after three distinct fix attempts,
  stop and escalate with the failing test, the three hypotheses tried, and the
  observed output.
- Gates: if a full `make lint` / `make test` run fails for a reason that looks
  environmental (compiler queue, toolchain download, another agent's load)
  rather than caused by this change, re-run once; if it fails the same way,
  record the log path and escalate rather than working around it.
- Ambiguity: if two readings of the acceptance criteria lead to materially
  different implementations, stop and present both with trade-offs.

## Risks

- Risk: a bounded tail is decoded from bytes whose leading edge is the middle
  of a multi-byte sequence, producing a replacement character or a
  UnicodeDecodeError at the very start of every truncated capture. Severity:
  medium Likelihood: high Mitigation: reuse the existing byte-by-byte
  incremental boundary walk in `cuprum/_echo_truncation.py` (`_encode_prefix`)
  rather than writing a second, divergent one; drive it with targeted tests on
  UTF-8, UTF-16-like, and ASCII inputs, and a Hypothesis property that the
  retained tail is always decodable and always a suffix of what a full capture
  would have produced.

- Risk: the spill file is left on disk when the run is cancelled, times out, or
  the child fails, leaking temporary files into `$TMPDIR` for the process's
  lifetime and beyond. Severity: medium Likelihood: medium Mitigation: register
  the accumulator's async cleanup in the same reconciliation choke point that
  already settles stream consumers (`_reconcile_run_tasks` in
  `cuprum/_subprocess_wait.py` and the pipeline teardown), composed through
  `_process_lifecycle._shielded_cleanup` so a cancellation cannot skip it; test
  each of success, non-zero exit, timeout, and cancellation by asserting the
  path no longer exists.

- Risk: a storage failure (full disk, unwritable directory, `EMFILE`) raises
  into the drain loop and turns a working child into a failed run, or kills the
  drain and hangs the child on a full pipe. Severity: high Likelihood: low
  Mitigation: the accumulator catches the write/flush error, stops writing,
  marks the metadata failed, records a `CaptureEvent` for emission, and lets
  the drain continue consuming the child's output to EOF. A dedicated test
  injects the failure through a spy accumulator.

- Risk: changing `_StreamConfig.capture_output` from `bool` to a policy type
  breaks one of the ~12 unit-test modules that construct it positionally.
  Severity: low Likelihood: high Mitigation: keep the field at positional slot
  #1 and give the new type a truthiness-compatible reading only where needed;
  grep every construction site before the edit and update them in the same
  commit.

- Risk: the `captures` field collides with the pinned positional-slot contract
  on `CommandResult`, silently re-binding `relay_fallbacks` for callers who
  pass seven positional arguments. Severity: high Likelihood: medium
  Mitigation: declare `captures` `kw_only` and place it *after*
  `relay_fallbacks`, so `relay_fallbacks` keeps both the seventh positional
  slot and its position; update `cuprum/unittests/test_public_api.py`'s tail
  assertions deliberately and keep the seven-positional-argument binding check.

- Risk: a pipeline stage's bounded capture accidentally bounds what the next
  stage receives, breaking stream fidelity. Severity: high Likelihood: low
  Mitigation: the relay path is architecturally separate; add an explicit
  behavioural test that runs a truncated-capture pipeline and asserts the
  downstream stage saw the complete stream.

## Progress

- [x] (2026-10-14) Reconnaissance: read every file Tasks 1–4 touch; identify
  the seams (`_streams._deliver_chunk`, `_finish_drain`,
  `_spawn_stream_consumers`, `_pipeline_config._build_stream_config`,
  `_reconcile_run_tasks`, `_shielded_cleanup`, `_encode_prefix`).
- [x] (2026-10-14) Reconcile the four path mismatches in the source plan and
  the `captures`/`relay_fallbacks` slot conflict; record in `Decision log`.
- [x] (2026-10-14) Prerequisite refactor: move the deprecated output
  spellings out of `cuprum/sh/output.py` into `cuprum/sh/_deprecated.py`,
  clearing the `C0302` module-length failure the capture work triggered (494 →
  318 lines). Landed as its own atomic commit before the feature, per
  AGENTS.md. Evidence:
  `uv run pytest cuprum/unittests/test_pipeline_output_options.py`
  `cuprum/unittests/test_safe_cmd_output.py`
  `cuprum/unittests/test_idle_heartbeat.py`
  `cuprum/unittests/test_public_api.py -q` reports `138 passed, 12 skipped`;
  `uv run ruff check cuprum/sh/ cuprum/__init__.py` passes; ADR-007 gains an
  addendum recording the split.
- [x] (2026-10-14) Task 1: capture-policy vocabulary, `RunOutputOptions` fields
  and resolution, `captures` on `CommandResult`, `PipelineResult.captures`,
  public exports. Evidence: `uv run pytest cuprum/unittests -q` reports
  `3282 passed, 90 skipped in 218.34s` with 27 snapshots passing;
  `uv run pytest --doctest-modules cuprum/capture_policy.py -q` reports
  `3 passed`; the per-stream independence property was mutation-checked (see
  `Verification plan`, O7).
- [x] (2026-10-14) Task 2: capture accumulators, `capture_events.py`,
  `capture_observation.py`, and the drain-loop wiring. The accumulators landed
  as `cuprum/_capture_accumulators.py`, `_capture_file.py`,
  `_capture_decode.py`, and `_capture_types.py` rather than the single
  `_capture_sink.py` the source plan named, because the module-length ceiling
  binds; the split seams are recorded in `Decision log`.
- [x] (2026-10-14) Task 3: thread policies through the single-command and
  pipeline paths; materialize results; cleanup on success/failure/timeout/
  cancellation; storage-failure handling. Landed across
  `_subprocess_stream_run.py`, `_subprocess_stream_post_exit.py`,
  `_capture_cleanup.py`, `_pipeline_results.py`, and
  `cuprum/sh/_capture_options.py`.
- [x] (2026-10-15) Task 4: unit tests, BDD feature and behaviour tests,
  documentation updates. Tests landed and green: `test_capture_cleanup.py`,
  `test_capture_accumulator.py`, `test_capture_spill.py`,
  `test_capture_spill_failure.py`, `test_capture_timeout.py`,
  `test_bounded_capture_drain.py`, `tests/behaviour/test_bounded_capture.py`
  with `tests/features/bounded_capture.feature`, and the runtime half of O9 in
  `test_public_api.py`. Non-vacuity measured for each (see
  `Surprises & discoveries`). Documentation landed in `docs/users-guide.md`,
  `docs/v0-2-0-migration-guide.md`, `docs/roadmap.md`, and
  `docs/cuprum-design.md`.
- [x] (2026-10-15) Clear the Pylint, spelling, and formatting findings the
  capture work introduced. `make pylint-classic` exits 0 with both passes at
  10.00/10; `make spelling`, `make check-fmt`, and `make markdownlint` each
  exit 0.
- [x] (2026-10-15) Clear the four df12 findings and the Skylos finding the
  `python-lint` chain only became able to reach once its earlier leaves passed.
  Three `R9104` trivial-attribute-wrapper findings in the new accumulators and
  one `R9101` prefer-structural-pattern-matching finding in
  `cuprum/sh/_capture_options.py` were repaired structurally, without
  suppression; `_is_positive_int` was modelled as a precise Skylos entry point
  after a probe isolated the scanner's `__post_init__` blind spot.
  `make python-lint` now completes with all six sub-targets passing — ruff,
  interrogate, pylint-classic, df12, ambrleaks, and skylos — which is the first
  run on this branch in which `ambrleaks` and `skylos` executed at all.
  Evidence: `/tmp/python-lint-cuprum-issue-443-...out` records HEAD `0bc470fb6`
  and `MAKE_EXIT=0`.
- [x] (2026-10-15) Clear the two guard-test failures the first full `make test`
  reached — the first run on this branch in which those two modules executed
  against the new code. `test_no_duplicate_module_definitions.py` was right
  about the `@typ.overload` stubs of `policy_kind`, which were deleted rather
  than exempted; `test_skylos_lint_contract.py` required the `_is_positive_int`
  entry point to be ratified in its typed register, which `a65330883` had
  missed. Landed as `1e8c687ed` with `make check-fmt`, `make python-lint` (all
  six leaves), and the four affected test modules green. Evidence:
  `/tmp/check-fmt-cuprum-issue-443-...out` and
  `/tmp/python-lint-cuprum-issue-443-...out` each record `MAKE_EXIT=0`;
  `/tmp/focused-capturefix-cuprum-issue-443-...out` records `46 passed`.
- [x] (2026-10-17) Rebase the 18-commit branch onto `origin/main`
  (`ad3ffc404`) before running gates. `main` had gained the nose duplication
  gate (`51697321e`) and the stateful harness repair (`065855728`) across
  fourteen files this branch also touches, and gate evidence only describes
  what CI will build if it is taken against the merge base. The rebase replayed
  every commit and resolved six conflicts: `weave` auto-resolved the run
  machinery against `main`'s unified `_prepare_stream_spawn`; the
  `_wait_for_streamed_process_exit` conflict was settled by keeping `main`'s
  deletion and moving our `_reconcile_shielded` helper beside the split
  post-exit module, so no duplicate definition survives; the migration guide's
  capture section was appended to the 0.2.0 feature list rather than dropped by
  the section renumbering; and the `pyproject.toml` dead-code entry points
  merged entry-by-entry. Backup branch `backup/issue443-pre-rebase-20261017`
  retains the pre-rebase tip.
- [x] (2026-10-17) Resolve the first post-rebase gate run's two findings, then
  repair the module-size regression the repair itself introduced. The run at
  `9de942193` passed `check-fmt` and `typecheck` and aborted `make lint` with
  two live failures: ten MD049 asterisk-emphasis spans across `CHANGELOG.md` and
  `docs/v0-2-0-migration-guide.md`, and eleven unsuppressed duplication
  families plus three stale allow entries. Three genuine extractions landed —
  `CaptureHookRegistration` onto `_TokenTupleRegistration`; the two spill
  storage-failure emitters into `_fail_storage`; and the capture-presence
  predicate shared by the run options and the pipeline configuration — with the
  remaining families recorded as reasoned members. **That last extraction pushed
  `cuprum/capture_policy.py` from 375 to 437 lines**, past the
  `max-module-lines = 400` ceiling, and the next run's `pylint-classic` failed
  with `C0302` before any later lint leaf could run. `3bbaafde1` moves the
  predicate, and the declared capture-setting union with it, to
  `cuprum/capture_presence.py`; `capture_policy.py` returns to 375 lines.
  Lesson: a duplication repair is a size change too, so the 400-line ceiling
  has to be re-checked after one, not just the duplication gate.
- [x] (2026-10-17) Run the full commit-gate set through `scrutineer` at the
  final HEAD, then push and open the draft pull request. All six gates are
  green at `b8b42fc27`, and each log records that head in its own header. The
  first post-rebase full run had never exercised the Rust nextest and doctest
  stages; this one did. Counts: pytest `3493 passed, 93 skipped`; nextest
  `167 tests run: 167 passed (1 slow), 0 skipped`; doctests
  `0 passed, 0 failed, 3 ignored` across all three crates.
- Lesson: a gate log's filename is not provenance. The `-6` logs are green and
  the `-7` `check-fmt` log is green, but the nearby `-6` `check-fmt` log is a
  *red* run at `c14cce55c`; reading filenames instead of each log's own `head=`
  line would have certified the wrong SHA. The headers were written
  inconsistently (`head=` first versus on the `# gate=` line), so both forms
  have to be read.
- [x] (2026-10-17) Clear the eleven findings from the first post-rebase
  CodeRabbit review, after the deterministic gates were green. Six needed code
  or test changes and are recorded individually in `Surprises & discoveries`;
  the other five were documentation, allowlist, or plan corrections.
  Non-vacuity was measured for all three code fixes by reverting each in turn.
- [x] (2026-10-17) Witness the pipeline after-hook cleanup directly.
  `cuprum/unittests/test_pipeline_capture_cleanup.py` is new: it spills on a
  completed pipeline and on one whose after-hook raises, and asserts the spill
  directory is empty afterwards. Reverting `_pipeline_internals.py` to its
  pre-fix shape fails the after-hook case with a leaked file, so the finding's
  fix is now covered by a test rather than only by the review argument.

## Surprises & discoveries

- Observation: the source coding plan names four paths that do not exist in
  this repository. Evidence: `cuprum/sh.py`, `docs/migration-0.2.0.md`,
  `_SubprocessExecution` in `cuprum/_subprocess_execution.py`, and
  `cuprum/_line_stream.py` are all absent; the real homes are
  `cuprum/sh/output.py`, `cuprum/sh/results.py`,
  `docs/v0-2-0-migration-guide.md`, `cuprum/_subprocess_spawn.py`, and
  `cuprum/_line_stream/coordinator.py`. Impact: the ExecPlan substitutes the
  real paths; the described work is unchanged.

- Observation: `_SubprocessExecution` is *defined* in
  `cuprum/_subprocess_spawn.py` and re-exported from
  `cuprum/_subprocess_execution.py`. Impact: the field edit goes in the spawn
  module, and the re-export needs no change.

- Observation: `cuprum/logging_hooks.py` already guards the external `.stdout`
  reader with `len(result.stdout) if result.stdout is not None else 0`, so the
  plan's requirement there is already satisfied. Impact: verify with a test
  rather than editing; record the verification in `Progress`.

- Observation: no module under `cuprum/` imports `tempfile` today; the only
  in-repo durable-temporary-file precedent is
  `benchmarks/ratchet_history_persistence.py`. Impact: the spill-file code is
  the first of its kind in the package and should carry a comment explaining the
  `delete=False` / `unlink(missing_ok=True)` pairing.

- Observation: `CommandResult` declares `resolved_path` *before*
  `relay_fallbacks` expressly so `relay_fallbacks` stays the declaration tail;
  the plan asks for `captures` to go *after* it. Impact: the tail pin in
  `test_public_api.py` must change for `fields[-1]` (now `captures`) while
  `positional[-1]` stays `relay_fallbacks`. See `Decision log`.

- Observation: `capture_policy.policy_kind` could not be the `StrEnum` call
  `CapturePolicyKind(policy)` it first looked like. Evidence: `enum.StrEnum`
  classifies by *value*, not by class, so `CapturePolicyKind(FullCapture())`
  raised `ValueError: FullCapture() is not a valid CapturePolicyKind` across 9
  tests. Impact: the mapping is a real function over the three types, raising
  `TypeError` for anything else — which is also what makes the module's public
  surface honest about a caller that bypassed the type checker.

- Observation: `uv run` provisions a `.venv` with CPython 3.14.7 although
  `requires-python` is `>=3.12` and the system interpreter is 3.12.14. Impact:
  any behaviour that differs between the two (error-message text, `StrEnum`
  repr, decoder details) is being validated on 3.14 locally; the ExecPlan's
  tolerance for "a test passes locally but fails in CI's pinned interpreter" is
  the existing repository contract, not a new risk introduced here.

- Observation: the maturin wheel-content snapshot
  (`cuprum/unittests/__snapshots__/test_maturin_build.ambr`) enumerates every
  shipped module, so a new module under `cuprum/` fails that test until the
  snapshot is updated. Impact: each new module this plan adds
  (`capture_policy.py`, `_capture_accumulators.py`, `_capture_decode.py`,
  `_capture_file.py`, `_capture_types.py`, `capture_events.py`,
  `capture_observation.py`) costs one `--snapshot-update` run on
  `test_maturin_build.py`. Adding the module and updating the snapshot belong
  in the same commit or the suite is red in between.

- Observation: `cuprum/sh/output.py` was already at 379 lines of pylint's
  enforced `max-module-lines = 400` before this plan touched it; the capture
  fields, resolution, and `resolved_capture` property took it to 494 and
  `make pylint-classic` reported `C0302: Too many lines in module (494/400)`,
  failing `make lint`. Impact: an unplanned prerequisite refactor. The cap
  applies to production targets only — `PYLINT_TEST_TARGETS` is invoked with
  `--disable=too-many-lines`, and every other file in the tree over 400 lines
  is a test file. Per AGENTS.md ("Separate atomic refactors … implement it in a
  separate atomic commit") the split lands first, so the capture feature is a
  clean addition on top of a compliant module rather than a change that leaves
  the gate red. See `Decision log` and the M1 milestone's recovery path.

- Observation: `cuprum/sh/__init__.py` re-exports `sinks`, `typ`, `warnings`,
  and `dc` as module attributes purely to preserve the former `cuprum/sh.py`
  import surface; no code in the tree — not the tests, not the scripts, not the
  benchmarks — reads any of them through the facade. Impact: the pair
  `typ`/`sinks` stays because `output.py` still defines them and the facade
  mirrors `output.py`'s namespace, but the `warnings` re-export became dead
  once the deprecated shim moved out and was removed with it rather than
  carried forward as a facade name with no reader.

- Observation (non-vacuity, M3): the end-to-end spill-cleanup suite
  (`cuprum/unittests/test_capture_spill_cleanup.py`) does **not** witness the
  sink path in `cuprum/_capture_cleanup.py`. Mutating
  `_FileAccumulator.cleanup` to `handle.close(remove=False)` — disabling the
  sink step's removal entirely — left all 10 tests passing, because the
  drain-result step removes the same file and either path alone is sufficient
  on every route the suite covers. Disabling the drain-result step in
  `_finalize_drain_handles` likewise left all 10 passing. Only disabling *both*
  produced failures (7 of 10). The sink step's own contract is therefore
  unwitnessed by that suite, and a regression that dropped it would have been
  invisible until the specific route it exists for — a drain cancelled
  mid-await — was exercised. Closed by adding
  `cuprum/unittests/test_capture_cleanup.py`, whose
  `test_a_sink_still_reaches_its_file_when_no_result_carried_the_handle` passes
  an empty drain tuple so the result step has nothing to act on and the sink
  step is the only thing that can remove the file. Under the same mutation that
  case and `test_cleanup_removes_the_file_a_spilled_sink_owns` now fail (2 of
  6). Date/Author: 2026-10-14, implementation agent.

- Observation (M3, O9): the obligation's wording — "a run with no capture
  configured still produces empty `captures` and complete `stdout`/`stderr`
  text" — describes two different runs, not one. A default `RunOutputOptions()`
  resolves both streams to `FullCapture` and reports two `full`/`memory`
  records; it is `capture=False` that reports `captures == ()` with `stdout` and
  `stderr` both `None`. The verification was split into two tests in
  `cuprum/unittests/test_public_api.py` so each run's real shape is pinned:
  `test_a_default_run_still_returns_complete_text_under_full_capture` and
  `test_a_run_that_disables_capture_reports_neither_text_nor_records`. The
  source plan's phrasing would have been satisfied by asserting either one
  alone and would have been wrong about the other. Date/Author: 2026-10-14,
  implementation agent.

- Observation (M3, O8): partial output surviving a timeout is carried by the
  drain *payloads* handed to `_handle_stream_timeout`
  (`cuprum/_subprocess_stream_run.py:132` and `:306`), not by `_finish_drain`'s
  non-EOF branch nor by `_finalize_capture_handles`. Two mutations that a
  reading of the design would predict to bite did not: replacing
  `_finish_drain`'s non-EOF branch with an empty capture, and skipping both
  calls inside `_finalize_capture_handles`, each left all five timeout cases
  passing. Passing `stdout_text=None, stderr_text=None` at both
  `_handle_stream_timeout` call sites failed 4 of 5. This is recorded because
  it locates the responsibility precisely: the readers reach EOF after the
  child is signalled, so the payload arrives by the EOF path, and only the
  handler's arguments decide whether it survives to the caller. Date/Author:
  2026-10-14, implementation agent.

- Observation (M4, O10): the documentation harness
  (`tests/behaviour/test_documentation_examples_behaviour.py`) executes every
  `tested-example` Python fence in `README.md`, `docs/users-guide.md`, and
  `docs/v0-2-0-migration-guide.md`, and requires each to contain an `assert`.
  `docs/cuprum-design.md` and `docs/roadmap.md` are **not** scanned. Impact:
  prose in the design document and roadmap is unreviewed by the harness, so it
  was checked by hand, and that is where the false claim below had survived.

- Observation (M4, O11): three documented claims were **wrong** and were found
  by running the code, not by reading it. (a) The design document, the roadmap,
  and the users' guide all stated that naming the `capture` shorthand together
  with a per-stream policy raises `ValueError`. It does not: a stream left at
  `None` inherits the shorthand and an explicit value wins for its own stream,
  exactly as `echo_stdout`/`echo_stderr` already resolve from `echo` — verified
  both ways. (b) The users' guide implied `storage_failed=True` by itself
  distinguished a complete capture from an incomplete one. A failed *create*
  leaves the stream in memory, complete, with `storage == "memory"` and
  `storage_failed=True`; a failed *append* gives `storage == "file"`. It is the
  pair that distinguishes them, and four tests in `test_capture_spill_failure`
  pin exactly that pairing. (c) The migration guide's first example captured
  both streams and then wrote `(record,) = result.captures`, which raises
  `ValueError` because capturing stderr too contributes a second record. The
  harness would have caught (c); only running the code caught (a) and (b).
  Date/Author: 2026-10-14, implementation agent.

- Observation (M4, O12): issue #443 pushed four production modules over the
  400-line ceiling — `_streams.py` 407, `_subprocess_stream_run.py` 423,
  `sh/output.py` 408, `_line_stream/coordinator.py` 428 — which `make lint`
  reports as pylint C0302 (`pyproject.toml` sets `max-module-lines = 400` with
  the comment "Match the project file-size ceiling enforced during review").
  Impact: four splits were required. The seam for each was chosen by evidence:
  an AST sweep for every `setattr`/`patch` call naming each module found the
  outside-patch sites, and a seam was only taken when the moved code's callers
  travelled with it or when the moved name is re-imported at the original site.
  A three-module probe confirmed the premise the `_subprocess_stream_run` split
  depends on: a name *defined* in A and *re-imported* into B is still read from
  B's globals by B's own callers, so patching `B.name` keeps working.
  Date/Author: 2026-10-14, implementation agent.

- Observation (M4, O13): moving `_start_line_stream_run` to
  `cuprum/_line_stream/spawn.py` **did** break a patch seam, and the break was
  the useful signal. `test_lines_stamp_events_from_the_spawn_clock` injected
  the spawn clock by patching `coordinator.perf_counter`. The sampling moved
  with the function, so a re-import into `coordinator` would have left the test
  patching a name nothing reads — it would have kept passing while testing
  nothing. The test now patches `spawn.perf_counter`; a mutation check confirms
  the seam is live (injecting 30.0 in place of 40.0 shifts the reported line
  timestamp to 13.25, exactly 43.25 − 30.0), and patching the old target now
  fails with `AttributeError` rather than passing vacuously. Date/Author:
  2026-10-14, implementation agent.

- Observation (M4, gate): the Pylint classic gate runs its *test-target* pass
  over `cuprum/unittests`, so the enabled `C1803`/`C1804`
  (`use-implicit-booleaness-not-comparison` and its string sibling) and `R0913`
  apply to the new capture test modules. Eleven findings, all introduced here;
  exit 24 (16 convention + 8 refactor). A local run that omitted
  `--disable=too-many-lines` also reported `C0302`, which is **not** a gate
  failure — `Makefile:449` passes that flag for `PYLINT_TEST_TARGETS`, whose
  targets are `cuprum/unittests scripts/tests tests/behaviour tests/features`.
  Impact: `_spill` in `test_capture_spill.py` was restructured to take the
  `FileBackedCapture` it was already repacking, dropping it to four arguments
  rather than suppressing `R0913`. Date/Author: 2026-10-15, implementation
  agent.

- Observation (M4, gate): the classic Pylint pass runs under **PyPy 8.0.0 /
  Python 3.12**, not CPython — `verify-classic-pylint` asserts
  `("pypy", (3, 12), (8, 0, 0))` before the pass runs. Measuring with
  `uv tool run --python 3.12` uses CPython 3.12, a different interpreter for a
  gate that pins PyPy. The exit code is the signal, not the "rated at N/10"
  line: a run can print nonzero findings and still read `10.00/10`, so a
  citation of the score alone would have hidden a failing gate. Date/Author:
  2026-10-15, implementation agent.

- Observation (M4, gate): the spelling gate caught Oxford violations that the
  earlier draft introduced. Four British spellings were written for verbs and
  nouns whose ending must be `-ize`/`-ization`, and three further words were
  the American or non-standard choice where the style guide requires the
  British one: the noun for a build output, the plural of "child", and the
  compound adjective formed from "hand" and "written". Naming the exact tokens
  here is what this note did at first, and that re-tripped the gate on its own
  text — the gate matches literal tokens, so a quoted violation is still a
  violation. The note therefore describes the classes rather than listing them.
  Verified against this branch's merge base rather than against a probe
  worktree: the sibling worktree available for comparison sat *ahead* of
  `8c78a5b7`, so its clean result measured a different tree and could not have
  supported the claim that these findings were pre-existing. Date/Author:
  2026-10-15, implementation agent.

- Observation (M4, gate): **a gate that aborts hides every leaf behind it, and
  the hidden leaves keep hiding each other.** `make python-lint` stops at its
  first failing sub-target, so while the classic Pylint leaf was red nothing
  downstream had ever executed on this branch. Fixing that leaf exposed four
  df12 findings; fixing those exposed a Skylos finding; only after all three
  rounds did `ambrleaks` and `skylos` both run to completion. The practical
  consequence is that "the gate I ran passes" is not evidence about a chained
  target until the chain reaches its end, and a single green run must be read
  from the log's own sub-target census rather than from a summary line. One
  build of this same trap: a background run whose *pipeline* exit code was
  reported as 0 while the tee'd log ended in
  `make: *** [Makefile:453: python-lint] Error 8`, because the shell reported
  the exit status of the final `tail` rather than of `make`. Date/Author:
  2026-10-15, implementation agent.

- Observation (M4, gate): two df12 rules are AST-shaped, and which spellings
  they tolerate is not inferable from reading the code. `R9104`
  (`trivial-attribute-wrapper`) flags a body that is a single call through an
  attribute chain rooted at a parameter — so `self._buffer.clear()`,
  `self._buffer.extend(chunk)`, and a module-level
  `def release(buffer): buffer.clear()` all trip it, while
  `self._buffer += chunk` and a rebinding `self._buffer = bytearray()` do not.
  `R9101` (`prefer-structural-pattern-matching`) flags consecutive `isinstance`
  guards on one subject, and a `match` with class patterns is its documented
  remedy. Both replacements were probed against the installed plugin before
  being applied rather than guessed at, because the obvious repairs — a helper
  function, or `.extend` in place of `.clear` — are themselves flagged. Date/
  Author: 2026-10-15, implementation agent.

- Observation (M4, gate): the Skylos dead-code leaf reported `_is_positive_int`
  as unused, and it is not. `BoundedCapture.__post_init__` and
  `FileBackedCapture.__post_init__` are its only callers, and the scanner's
  reference graph does not traverse a `__post_init__` body at all. Isolated
  with a probe package against skylos 4.33.2 on the Makefile's exact command
  line: a plain `__init__` and an explicitly written dataclass `__init__` are
  both followed, a generated dataclass `__init__` and a `__post_init__` are
  not, and `slots` and `frozen` were each ruled out as the trigger by
  rebuilding the same helper behind slotted and unslotted dataclasses. The fix
  is the documented one — a `type = "function"` entry point in
  `[tool.skylos.dead_code.entrypoints]` naming the symbol and recording the
  probe — not `make skylos-allow`, which is reserved for boundaries an
  entry-point rule cannot describe. Date/Author: 2026-10-15, implementation
  agent.

- Observation (M4, gate): two repo-owned guard tests failed the first full
  `make test` on this branch, and both were correct about defects the whole
  `lint` chain had passed. `test_no_duplicate_module_definitions.py` reported
  three sibling `def policy_kind` sites, and `test_skylos_lint_contract.py`
  reported that the `pyproject.toml` entry point added for `_is_positive_int`
  was absent from the typed register the test compares it against. Neither test
  file is touched by this branch, so both are pre-existing contracts the new
  code violated rather than tests that drifted. The register is a paired
  contract: `8c78a5b77` (#438) changed `pyproject.toml` and this test file in
  one commit, which is the convention the earlier commit here missed. Date/
  Author: 2026-10-15, implementation agent.

- Observation (M4, gate): the duplicate-definition guard was right about the
  `@typ.overload` stubs, and the honest repair was to delete them rather than
  to teach the guard about overloads. `typing.overload` leaves only the last
  `def` reachable at runtime, so the two stubs were literally the unreachable
  siblings the guard describes. No caller passes `None`: `_empty_capture_for`
  returns before the call when the policy is absent, and
  `CapturedStream.policy` is a non-optional `CapturePolicyKind`. The parameter
  was therefore widened for no caller, and the two
  `ruff: ignore[overload-with-docstring]` suppressions existed only to resolve
  the ruff/interrogate conflict the stubs created — narrowing the parameter
  removed the suppressions as well, which is the tell that they were
  speculative generality rather than a real contract. This is the same
  preference `cuprum/_streams_pump.py` records when it chooses a separate value
  over overloading a return. Date/Author: 2026-10-15, implementation agent.

- Observation (rebase): the 2026-10-17 rebase onto `origin/main` was not
  mechanical for two reasons worth recording. First, `main` had independently
  unified the three stream wiring sites behind `_prepare_stream_spawn`, which
  is the same seam this branch had reached from the capture side; the
  resolution kept upstream's helper and threaded the capture accumulators
  through it, so both improvements survive as one entry point rather than as
  two overlapping ones. Second, the `_wait_for_streamed_process_exit` conflict
  reported "modified in ours, deleted in theirs" because `339a3d8c0` had
  already moved that function into `_subprocess_stream_post_exit.py`; keep-both
  would have produced two definitions, and the duplicate-definition guard that
  caught the earlier `policy_kind` stubs would have caught these too. The
  `_reconcile_shielded` helper moved beside its only callers in the split
  module instead, leaving no re-export for the run module to shadow.
  Date/Author: 2026-10-17, implementation agent.

- Observation (rebase, 2026-10-18): the replay onto `d93df9563` is a
  *semantic* rebase, not a textual one, and the failures it produced are not
  the ones a conflict list predicts. Four are worth recording because each was
  green in its own parent and only failed once the two histories were combined.

  First, `main`'s PR #445 widened `_RunTaskOwnership.consumers` to
  `_ConsumerPair` with `None` slots, and both gather sites on this branch still
  destructured the result as a `_DrainPair`. The union is not merely a typing
  puzzle: both callers read `.payload` and `.capture` off each slot and feed
  the pair to `_finalize_capture_handles`, so a raw `None` would have failed
  downstream rather than at the type checker. The repair is a single
  `_decode_consumer_drains` helper in `cuprum/_stream_drain.py` that decodes
  both slots in stream order against one `_drain_context(..., preserve=True)`,
  owned by one function so no call site can decode one slot and forget the
  other.

  Second, `cuprum/sh/output.py` landed at exactly 400 lines after this branch's
  own split and the replay's extra content pushed it one over the pylint cap.
  The `resolved_capture` read moved beside `_resolve_capture` in
  `cuprum/sh/_capture_options.py`, which is where that widening already
  belonged. `cuprum/_subprocess_streams.py` was over for a different reason:
  the replay's resolution kept `main`'s wiring shape *and* this branch's
  `_resolve_stream_configs`, so `_resolve_stream_configs` and
  `_resolve_stream_sink` moved to `cuprum/_stream_config.py` with re-exports
  left behind, because `cuprum._command_internals` still reaches the sink
  resolver by its old path. `cuprum/capture_policy.py` is over the cap too, but
  it is over by exactly the same amount in the original commit `901ee8bcf` (437
  lines in both); the author's next commit `86d00d942` trims it to 375, so it
  is left for the replay to repair rather than repaired twice.

  Third, the replay of this branch's split commit took the *pre*-#445 body of
  `_await_stdin_writer`, which awaited the writer task directly. `main` had
  since replaced that with `await asyncio.shield(_settle_stdin_writer(...))`.
  The bare await let a producer parked in `anext` outlive the run's deadline,
  and three stdin tests that main had already repaired went red again. The
  settle call and its two-paragraph rationale were restored; the module
  docstring's "leaf with respect to the run module" claim still holds, because
  `_settle_stdin_writer` comes from `_subprocess_stdin_reclaim`, not from the
  run module.

  Fourth, and only visible after the third repair, `main`'s
  `_install_post_exit_probe` patches the settle where the name is *resolved*,
  and its docstring's premise — that both run loops resolve it from
  `_subprocess_stream_run` — is exactly what this branch's post-exit split
  invalidates. The direct path still resolves it there; the streamed path now
  resolves it in `_subprocess_stream_post_exit`. A single patch left the
  streamed path settling for real, so `probe.settling` never fired and both
  scenarios failed on their readiness wait. The probe now patches both
  resolution sites, which keeps it independent of which path a case exercises.
  The general lesson: a test that patches by resolved name is a contract on
  where production resolves it, and splitting a module silently moves that
  contract. Date/Author: 2026-10-18, implementation agent.

- Observation (review, bounded trim): the leading-edge trim was written for
  UTF-8 and applied to every encoding. Its loop skips bytes in the
  `0x80`-`0xBF` range, which is UTF-8's continuation range but is also an
  ordinary value for a UTF-16 low or high byte, so a bounded capture decoded
  from `utf-16-le` could lose real characters at its leading edge. Reproduced
  with a `U+0080` payload before fixing: the character vanished. The repair
  realigns a fixed-width encoding by *stream offset* — which is why
  `_BoundedAccumulator` now tracks `_offset` beside its buffer — and drops a
  leading low surrogate, because a cut that splits a surrogate pair leaves a
  half that no decoder can use. Any other encoding gets no leading adjustment.
  Date/Author: 2026-10-17, implementation agent.

- Observation (review, byte mode): a byte-exact capture is never decoded, so
  the trim had no business touching it: a binary payload legitimately ending in
  a continuation-shaped byte is not a broken character, and trimming it loses
  data the caller asked for. The trim is now applied only when the run decodes.
  A second case followed from the same reading: a stream that was never cut has
  no damaged edge, so it is handed back untouched too. Date/Author: 2026-10-17,
  implementation agent.

- Observation (review, spill lifetime): `CapturedStream.read_text` and the
  `path` attribute were documented as though the file outlived the run. It does
  not: the run removes it before returning unless the policy was built with
  `keep_file=True`, which is the same lifetime the cleanup tests assert. The
  docstrings, the changelog, and the users guide now say so, and name the
  byte-mode read-back as the one case where the file is read *before* it is
  removed. Date/Author: 2026-10-17, implementation agent.

- Observation (review, pipeline cleanup): `_finalize_capture_handles` ran after
  the result was assembled, not in a `finally`. Two exits therefore skipped it
  — a failing after-hook and a cancellation re-raised by the observer drain —
  and neither has another path that closes those handles. The cleanup now runs
  in a `finally` under `_shielded_cleanup`, so every exit owes the files. The
  new `test_pipeline_capture_cleanup.py` witnesses both exits; reverting the
  module to its pre-fix shape fails the after-hook case with a leaked file.
  Date/Author: 2026-10-17, implementation agent.

- Observation (review, spill retry): `_should_spill` asked only about the
  threshold, so a stream whose *create* had failed re-offered the spill on
  every later chunk. Each attempt cost a syscall and emitted a storage-failure
  event, turning one broken directory into a metrics flood. `_spill`'s own
  docstring already promised "one syscall rather than one per chunk", so the
  test that pinned per-chunk re-offering was pinning the wrong behaviour; it
  was renamed and its assertions inverted. The guard is safe because `_failed`
  is set only by a failed *creation*: a failed append leaves the path set, so
  `absorb` takes the append branch and never asks the question again.
  Date/Author: 2026-10-17, implementation agent.

- Observation (review, removal failures): `SpillHandle.close` suppressed a
  failed `close` but not a failed `unlink`, so a filesystem that refused the
  removal could replace the caller's own timeout or cancellation with a delete
  error. Removal is housekeeping and is now absorbed like every other storage
  call. `test_capture_cleanup.py` gains a `Path` subclass whose `unlink` always
  refuses, so the case is witnessed rather than assumed. Date/Author:
  2026-10-17, implementation agent.

## Decision log

- Decision: place `captures` after `relay_fallbacks` and mark it `kw_only`.
  Rationale: the source plan is explicit ("Add a trailing, defaulted field
  `captures: tuple[CapturedStream, ...] = ()` to `CommandResult` … placed after
  `relay_fallbacks`"), and `kw_only` is what stops it taking a positional slot.
  `relay_fallbacks` therefore keeps the seventh positional slot and remains
  `positional[-1]`, while `captures` becomes the new declaration tail
  (`fields[-1]`). Both facts are pinned in `test_public_api.py`; the second
  assertion is updated, the first is not. Date/Author: 2026-10-14,
  implementation agent.

- Decision: the policies also apply to `run_bytes()`; `BytesCommandResult`
  keeps its `bytes | None` fields and gains no `captures` field. Rationale:
  issue #443 is about the `str` result, and the first cut scoped the policies
  to the text path. That left `run_bytes()` silently ignoring a policy the
  caller had named, which the issue does not sanction. Applying them costs one
  branch — a spilled stream is read back — and the result type stays unchanged
  because there is no record to put a spill in. The `bytes | None` contract is
  therefore honest but silent: byte mode cannot distinguish a complete capture
  from a truncated one, and the read-back is O(stream size). Both limits are
  documented in `docs/users-guide.md`. Date/Author: 2026-10-14, implementation
  agent; rewritten after review.

- Decision: reuse `_echo_truncation._encode_prefix`'s incremental
  decoder/encoder walk instead of adding a second UTF-8 boundary finder.
  Rationale: the repository already has one correct implementation of "how many
  bytes of this buffer form complete code points", used by the echoed-line
  bound; a second one would drift. The helper is currently private to that
  module, so it is either imported directly (same package) or lifted into a
  shared private helper if the signature does not fit. Date/Author: 2026-10-14,
  implementation agent.

- Decision: the accumulator's cleanup is an async coroutine registered
  through `_process_lifecycle._shielded_cleanup` at the existing reconciliation
  choke points, not a `try/finally` inside the drain. Rationale:
  `_shielded_cleanup` is the repository's existing primitive for "this teardown
  must run even under cancellation", and `_reconcile_run_tasks` is already the
  single point every single-command outcome passes through. A drain-local
  `finally` would not run when the drain task is cancelled mid-await, which is
  exactly the case the acceptance criteria name. Date/Author: 2026-10-14,
  implementation agent.

- Decision: extend `CapturedStream` beyond the source plan's field list with
  `storage_failed: bool` (default `False`) and a derived `truncated` property,
  and make `dropped_bytes` default to `0` rather than being a required field.
  Rationale: the plan's Task 3 requires the storage-failure path to "mark the
  metadata as failed", which needs a field to mark; the plan's Task 4 requires
  asserting truncation, and deriving it from `dropped_bytes` means the two
  cannot disagree. A truthy default of `0` keeps `total_bytes - dropped_bytes`
  meaningful for every record, including one built by a test. Date/Author:
  2026-10-14, implementation agent.

- Decision: `RunOutputOptions` classifies an invalid `capture_stdout`/
  `capture_stderr` value with `isinstance(declared, FullCapture | BoundedCapture
  | FileBackedCapture)` against the *types* rather than against the
  `CapturePolicy` alias. Rationale: `CapturePolicy` is a `type` alias for a
  union, so `isinstance` accepts it in CPython 3.10+; naming the three types
  explicitly is the same check with no dependence on that. The `bool` check
  must come first because `bool` is not a member of the union either way;
  ordering it first is what makes `capture_stderr=True` resolve instead of
  raising. Date/Author: 2026-10-14, implementation agent.

- Decision: move `IOOptions`, `_DeprecatedOutputFlags`, and
  `_resolve_pipeline_output` into a new `cuprum/sh/_deprecated.py` before
  starting the capture feature, as a separate atomic commit. Rationale: the
  source plan's Task 1 says to update `cuprum/sh.py`, which does not exist; the
  real home, `cuprum/sh/output.py`, was at 379 of 400 lines and the Task 1
  additions took it to 494, which fails `make lint` outright (`C0302`). The
  repository's own precedent for this exact pressure is a split, recorded three
  times in ADR-007's addenda (2026-09-14, 2026-09-25, 2026-10-13), and
  AGENTS.md requires a refactor to be a separate commit from the functional
  change it enables. The moved block is the natural cut: it describes how to
  *reach* the options vocabulary rather than what the vocabulary means, and
  both surfaces are deprecated. Alternatives rejected: a module-size
  suppression (the ADR's addenda treat "no module-size suppression" as the
  standing contract, and the cap is what drove every previous split); splitting
  `RunOutputOptions`'s docstring out (the docstring is the public contract and
  belongs beside its fields); leaving the feature incomplete until a later
  milestone (the cap fails the gate immediately). Date/Author: 2026-10-14,
  implementation agent.

- Decision: `cuprum/sh/__init__.py` keeps re-exporting every moved name with the
  same object identity, and drops the `cuprum.sh.warnings` alias the moved
  `warnings` import had left behind. Rationale: the facade's own docstring
  promises that "every name previously defined or imported by the former
  `cuprum/sh.py` module remains importable from `cuprum.sh` with the same
  object identity", and `IOOptions` plus the two flag helpers are such names.
  `warnings` is a module alias, not a name the facade ever published in
  `__all__`, and nothing in the tree — tests, scripts, or benchmarks — reads
  `cuprum.sh.warnings`; carrying it forward would be a facade name with no
  reader, which is compatibility theatre. `typ` and `sinks` stay because
  `output.py` still defines them and the facade mirrors that module's
  namespace. Date/Author: 2026-10-14, implementation agent.

- Decision: `capture_stdout` and `capture_stderr` are declared `kw_only` even
  though they read beside `capture` in the source plan's prose. Rationale:
  `RunOutputOptions` is a public, non-`kw_only` dataclass and
  `test_public_api.py` pins its first six positional slots
  (`max_echo_line_bytes` and `on_line` among them). Declaring the new fields
  positionally would insert them into that sequence and rebind every existing
  positional caller — on a pre-1.0 API with external callers in the wild, that
  is a silent break rather than a documented migration. `kw_only` is the
  mechanism the repository already uses for this exact situation on
  `CommandResult`. Date/Author: 2026-10-14, implementation agent.

- Decision: extract the convenience-flag validation and sink synthesis —
  `_validate_convenience_flags` and the former `_synthesize_sink_from_flags`
  method — into a new `cuprum/sh/_convenience_sink.py`, as a second separation
  inside the same prerequisite refactor. Rationale: clearing the deprecated
  spellings took `cuprum/sh/output.py` to 318 lines, but the capture work takes
  it to 431, which fails `C0302` a second time against the same
  unreachable-targets-only cap. Measured: the two pieces are 58 lines together
  — 26 for the validation and 32 for the synthesis — and removing them leaves
  `output.py` at 370 with 30 lines of headroom. They are one responsibility:
  what the `group`/`annotate_failure` flags *produce*, as against what the
  options *are*. They form no cycle with `output.py`: the new module imports
  only `GitHubActionsSink` and the `TYPE_CHECKING`-guarded `RunOutputOptions`,
  and `__post_init__` calls them at run time. The ``_synthesized_sink``
  identity field stays declared on the dataclass, since it is what
  `dataclasses.replace` forwards and so what tells a synthesized sink from a
  caller-supplied one. Alternatives rejected: importing `sinks.base.OutputSink`
  at run time in `output.py` purely to satisfy a type checker (that import
  exists for the public `sink` field's annotation and would otherwise stay
  `TYPE_CHECKING` — changing a module's runtime import set to shave lines is
  the wrong trade); a `too-many-lines` suppression (the same standing contract
  the first split declined); moving `resolved_capture` out (the plan requires
  it "mirroring `resolved_echo`", which puts it beside its sibling).
  Date/Author: 2026-10-14, implementation agent.

- Decision: `_cleanup_capture_sinks` propagates the first cleanup failure
  rather than absorbing it. Rationale: it runs under the caller's
  `_shielded_cleanup`, which is where a genuine teardown error belongs; the
  established contract for the capture paths is that a *storage* failure is
  absorbed into metadata (see `_capture_file.py`'s module docstring) while a
  programming or dispatch error is not. A sink that fails to clean up is
  reporting a broken filesystem, which the caller can act on and the metadata
  cannot express. Date/Author: 2026-10-14, implementation agent.

- Decision: split the four over-cap modules at these seams —
  `_StreamConfig` to `cuprum/_stream_config.py`, the three post-exit awaits to
  `cuprum/_subprocess_stream_post_exit.py`, `_resolve_capture` to
  `cuprum/sh/_capture_options.py`, and `_start_line_stream_run` to
  `cuprum/_line_stream/spawn.py`. Rationale: each seam removes a cohesive unit
  whose callers either travel with it or read it through a re-export, so no
  outer patch seam moves. `_StreamConfig` is read by roughly twenty importers
  through the `cuprum._streams` re-export; the post-exit helpers are
  re-imported into the run module *because* their callers stayed there, which
  the re-export probe confirmed is what keeps those patch sites live; nothing
  imports `_resolve_capture` externally; and the starter belongs beside the
  three builders it drives. Two alternatives were declined: moving
  `_finalize_capture_handles` (its caller set is broader than the split needs)
  and moving any name whose only purpose would be to make a milestone viable.
  Date/Author: 2026-10-14, implementation agent.

- Decision: `CapturePolicy` becomes a `TYPE_CHECKING`-only import in
  `cuprum/sh/output.py` while `sinks` stays a run-time import carrying a
  `ruff: ignore[typing-only-first-party-import]`. Rationale: the comment on
  `sinks` says it must stay resolvable from the module's globals for
  `typing.get_type_hints`. That invariant is *already* not satisfied —
  `get_type_hints(RunOutputOptions)` raises
  `NameError: name 'LineHook' is not defined`, because `LineHook` is already
  type-only — so extending the existing pattern for `CapturePolicy` neither
  breaks a working contract nor newly violates one. Claiming the `sinks` rule
  applies to `CapturePolicy` would have been the error. Date/Author:
  2026-10-14, implementation agent.

- Decision: repair the four df12-pylint findings by changing the shape of the
  code rather than by suppressing the messages — `absorb` appends with
  `self._buffer += chunk` instead of `self._buffer.extend(chunk)`, both
  `cleanup` methods rebind `self._buffer = bytearray()` instead of calling
  `.clear()`, and `_resolve_capture` dispatches on `declared` with a `match`
  statement instead of an `isinstance` chain. Rationale:
  `docs/developers-guide.md` instructs the implementer to "repair the
  assertion, alias, suppression rationale, or dispatch structure instead" of
  disabling a df12 message, and the mechanical trap makes the alternative worse
  than merely inelegant — a `# pylint: disable=` pragma cannot silence a df12
  message, and because the classic PyPy-backed pass runs without the plugin it
  reports `W0012: unknown-option-value` at each site and aborts the leaf with
  `Error 20` *before* the df12 pass the pragma was meant to satisfy ever runs.
  Neither rewrite changes behaviour: `+=` and `.extend` both append in place,
  and rebinding releases the same buffer `.clear()` would have emptied. The
  tolerances were established by probing the installed plugin with
  purpose-built files rather than by reading the rule and guessing, because the
  plausible first repairs are themselves flagged — extracting `release(buffer)`
  as a module-level helper trips `R9104`, and `self._buffer.extend(chunk)`
  inside a one-statement method trips it too. Date/Author: 2026-10-15,
  implementation agent.

- Decision: clear the skylos `SKY-U001 unused function: _is_positive_int`
  finding with a `[[tool.skylos.dead_code.entrypoints]]` rule of
  `type = "function"` and the fully qualified symbol name, rather than with
  `make skylos-allow` or by deleting the helper. Rationale: the finding is a
  false positive with a proven mechanism, and `AGENTS.md` prescribes the
  entry-point rule first for exactly this case — an implicit runtime caller the
  scan cannot model — reserving the named allow-list exception for boundaries
  an entry-point rule cannot describe. `_is_positive_int` is called twice, from
  `BoundedCapture.__post_init__` and `FileBackedCapture.__post_init__`, and
  both calls are covered by tests; a probe matrix against skylos 4.33.2
  established that the reference graph follows a plain class `__init__` and an
  explicitly written dataclass `__init__` but *not* a generated dataclass
  `__init__` or a `__post_init__` body, with `slots=True` and `frozen=True`
  each ruled out as the trigger. Deleting a used helper to satisfy a scanner
  would have been the error the rule exists to prevent. The `reason` field
  records the call sites, the probe result, and the two covering tests, so a
  later reader can re-derive the ruling. Date/Author: 2026-10-15,
  implementation agent.

- Decision: delete the two `@typ.overload` stubs of `policy_kind` and narrow
  its parameter to a concrete `CapturePolicy`, and ratify the
  `_is_positive_int` entry point in `_RUNTIME_FUNCTION_ENTRY_POINTS`.
  Rationale: the first failure was caught by a guard whose module docstring
  exists precisely to report unreachable sibling definitions, and
  `typing.overload` makes the stubs exactly that — only the final `def` is
  reachable at runtime — so the guard was correct and the code was wrong.
  Widening a parameter no caller uses is not a contract worth preserving, and
  the proof is that narrowing it removed both
  `ruff: ignore[overload-with-docstring]` suppressions along with it; a real
  contract does not disappear when its only obstacle does. The second failure
  is a paired contract that `8c78a5b77` (#438) honoured by changing
  `pyproject.toml` and the register in one commit, and that `a65330883` broke
  by changing only one side. The test's own nullable call sites now route
  through a local `_kind` helper, so the test states the distinction the drain
  implements — an uncaptured stream is the absence of a call, not a policy that
  classifies to `None`. Date/Author: 2026-10-15, implementation agent.

## Outcomes & retrospective

Delivered: capture is a per-stream policy. `RunOutputOptions` gained
`capture_stdout` and `capture_stderr`, each naming one of `FullCapture`,
`BoundedCapture(max_bytes=N)`, or `FileBackedCapture(...)`, with a stream left
at `None` inheriting the existing `capture` shorthand so every current caller
resolves exactly as before. `CommandResult.captures` and
`PipelineResult.captures` report what each stream's policy actually did —
`policy`, `storage`, `total_bytes`, `dropped_bytes`, `truncated`, `path`,
`owned`, `storage_failed` — and `capture_events.py` plus
`capture_observation.py` expose the same facts as observable events.

The acceptance criterion that drove the design is met and witnessed: a bound
never reaches the relay. `tests/behaviour/test_bounded_capture.py` runs real
two-stage pipelines and reads the *second* stage's verdict, so a bound that had
leaked into what a stage hands downstream would show as a shortened count
rather than as a capture nothing compares against.

Retrospective. Three lessons are worth carrying forward.

The first is that a gate's *exit code* is the evidence, not its summary line. A
Pylint run can print nonzero findings and still report `10.00/10`, and the
classic pass runs under PyPy — not the CPython the same command selects by
default. Reading the score would have shipped a failing gate.

The second is that a comparison tree must be *the* baseline, not merely *a*
tree. A sibling worktree was available that appeared to be main, and measuring
it produced a clean result that would have supported a false claim that the
findings were pre-existing. It sat ahead of this branch's merge base, so it was
a different tree; the claim was withdrawn and the findings fixed as this
branch's own. The same trap had already been sprung twice earlier in this task
(a wrong `ty` version, then an inherited diagnostic count), which is why the
baseline is now named by commit rather than by an available checkout.

The third is that document harnesses have blind spots worth knowing.
`tests/behaviour/test_documentation_examples_behaviour.py` executes the
`tested-example` fences in `README.md`, `docs/users-guide.md`, and
`docs/v0-2-0-migration-guide.md`, but not those in `docs/cuprum-design.md` or
`docs/roadmap.md`. Three documented claims about this feature were wrong and
only running the code found them.

Known limitation, documented rather than fixed: `FileBackedCapture` in
byte-exact mode reads the whole spill file back to return the child's bytes,
which is O(stream size) and so does not carry the bounded-memory guarantee that
text mode does. This is stated in `docs/users-guide.md`.

## Context and orientation

Cuprum is a Python subprocess-orchestration library. A caller builds a typed
command with `cuprum.sh.make`, wraps the run in `scoped(ScopeConfig(...))` to
allow the programs, and calls `run_sync()`/`run()`. Output handling is
configured by `RunOutputOptions` (`cuprum/sh/output.py`), and the outcome is a
`CommandResult` or `PipelineResult` (`cuprum/sh/results.py`).

The run's stream handling is a *tee*: one drain task per stream reads chunks
from the OS pipe and feeds three independent taps — a capture buffer, an echo
sink, and a line observer. `cuprum/_streams.py` owns that loop
(`_deliver_chunk` feeds all three) and `cuprum/_stream_drain_finish.py` owns
its teardown (`_finish_drain` decides what a cancelled or EOF-terminated drain
hands back).

Terms used below:

- *Capture*: retaining the child's bytes for the caller, as opposed to echoing
  them to a presentation sink. Only capture reaches `CommandResult.stdout`.
- *Tail*: the most recent N bytes of a stream, dropping the oldest first.
- *Spill*: moving a stream's retained bytes out of the parent heap and into a
  temporary file, so the parent's memory stops tracking the child's total
  output.
- *Relay*: the pump that forwards one pipeline stage's stdout into the next
  stage's stdin. It is not capture, and no capture policy may affect it.
- *Shorthand resolution*: `echo` filling in `echo_stdout`/`echo_stderr` when
  those are left `None`. The new capture fields resolve the same way.
- *Finalize*: turning an accumulator's retained state into the pair (public
  value, `CapturedStream` metadata) at the end of a drain.

## Conformance basis

The upstream artefacts for this work are:

- Issue #443, "Add bounded and file-backed capture policies per stream"
  (`https://github.com/leynos/cuprum/issues/443`), which supplies the
  acceptance criteria.
- The coding plan delivered with the task, held verbatim at
  `/tmp/issue443-task.md`, which supplies the four-task decomposition.
- `docs/cuprum-design.md` §7.4, "Tee and Output Observation", which currently
  states that "Capture stays a single joint switch" and must be updated.
- ADR-002 (echo and tee acceleration), which phases 6 and 7 of
  `docs/roadmap.md` descend from and which issue #443 is deliberately distinct
  from — phases 6 and 7 accelerate the *existing* full-capture and echo paths,
  they do not introduce bounds.
- `docs/v0-2-0-migration-guide.md`, which records additive changes to
  `RunOutputOptions` and `CommandResult`.
- `docs/documentation-style-guide.md` and the repository `AGENTS.md`, which
  govern prose and code style.

Trace links:

```plaintext
ISSUE-443-AC-bounded-capture      -> EP-T1 -> EP-T2 -> tests/behaviour/test_bounded_capture.py::bounded tail
ISSUE-443-AC-per-stream           -> EP-T1 -> cuprum/unittests/test_capture_policy.py
ISSUE-443-AC-relay-untouched      -> EP-T2 -> EP-T3 -> tests/behaviour/test_bounded_capture.py::downstream stream complete
ISSUE-443-AC-partial-on-failure   -> EP-T3 -> cuprum/unittests/test_capture_timeout.py
ISSUE-443-AC-storage-failure      -> EP-T3 -> cuprum/unittests/test_capture_spill_failure.py
ISSUE-443-AC-unicode-boundary     -> EP-T2 -> cuprum/unittests/test_bounded_capture_drain.py
ISSUE-443-AC-deterministic-cleanup-> EP-T3 -> cuprum/unittests/test_capture_spill_cleanup.py
ISSUE-443-AC-default-preserved    -> EP-T1 -> cuprum/unittests/test_public_api.py
```

## Verification plan

Obligations introduced or preserved by this change.

**O1 — Bounded tail retains a suffix and never splits a code point.**
Invariant: for any byte stream `S` and any `max_bytes = n > 0`, the retained
bytes equal the last `m` bytes of `S` for some `m` in `(0, n]`, and the union
of retained and dropped bytes is exactly `S` in order; the retained bytes are a
decodable suffix (they start on a code-point boundary). Lemma:
`dropped_bytes + len(retained) == total_bytes`. Method: property test with
Hypothesis over generated byte strings and bounds, plus parameterized cases for
the boundaries `n = 1`, `n` exactly equal to the stream length,
`n = len(S) - 1`, and `n > len(S)`. Artefact:
`cuprum/unittests/test_bounded_capture_drain.py`. Evidence: hypothesis reports
`Falsifying example` before the accumulator handles multi-byte input; all cases
pass after. Non-vacuity: generators must reach multi-byte UTF-8 (assert a
classification counter that at least one generated case contains a code point
of >1 byte) and must exercise both the "under budget" and "over budget"
classes; the negative control is a deliberately off-by-one bound
(`max_bytes = n + 1`) which the `dropped_bytes` lemma rejects.

**O2 — Full capture is byte-for-byte unchanged.** Invariant: with
`FullCapture()`, the value returned is exactly today's
`buffer.decode(encoding, errors)` for the same input. Method: parameterized
comparison test that drives the drain once through the old `bytearray` path
semantics and once through the accumulator, asserting equality across a fixed
corpus (ASCII, UTF-8 multibyte, empty stream). Artefact:
`cuprum/unittests/test_capture_accumulator.py`. Evidence: passes; would fail if
the accumulator reordered, dropped, or re-decoded bytes. Non-vacuity: the
corpus includes a non-empty multibyte case and the empty stream, so neither the
"same" nor "both empty" outcome is produced by a degenerate input set.

**O3 — File-backed capture writes the complete stream and reports byte
counts.** Invariant: the spill file's contents equal the full `total_bytes` of
the stream, byte for byte, and the finalized metadata reports
`storage == "file"`, `owned is True`, and a real path, with `stdout is None`.
Method: parameterized test driving the drain with a file-backed policy over
corpora both below and above `spill_threshold_bytes` (asserting a file exists
in the above-threshold case and that the below-threshold case still reports
`storage == "memory"`). Artefact: `cuprum/unittests/test_capture_spill.py`.
Evidence: the below-threshold case would fail if the accumulator spilled
eagerly; the above-threshold case would fail if it truncated the file.
Non-vacuity: the below-threshold corpus is non-empty, so "no file written"
cannot come from "nothing captured".

**O4 — A storage failure is non-fatal and is reported.** Invariant: when the
spill write or flush raises `OSError`, the drain continues to EOF, the run
returns a result, and a `CaptureEvent` with category `STORAGE_WRITE_FAILED` or
`STORAGE_FLUSH_FAILED` is emitted carrying only the exception's class name.
Method: injected-fault test with a spy/spilled accumulator whose write raises,
subscribed through `observe_capture`. Artefact:
`cuprum/unittests/test_capture_spill_failure.py`. Evidence: the run raises
nothing and the emitted event's `error_type` is `"OSError"`; without the catch
the test fails with the injected exception propagating out of the drain.
Non-vacuity: the injected fault is a real raised `OSError`, not a mocked return
value, so the except clause is genuinely exercised.

**O5 — Owned spill files are removed on every outcome.** Invariant: for
success, non-zero exit, timeout, and cancellation, a run with
`FileBackedCapture(keep_file=False)` leaves no file at the reported path; with
`keep_file=True` the file survives with complete contents. Method:
parameterized end-to-end test over the four outcomes, asserting `Path.exists()`
afterwards. Artefact: `cuprum/unittests/test_capture_spill_cleanup.py`.
Evidence: the timeout and cancellation cases fail before the cleanup is
registered through `_shielded_cleanup`. Non-vacuity: each case first asserts
the file *existed* during the run (via the reported metadata path), so "not
there afterwards" cannot be satisfied by never having written it.

**O6 — A bound never reaches the relay.** Invariant: in a two-stage pipeline
where stage 1's stdout is bounded, stage 2 observes stage 1's complete stream.
Method: behavioural test running a producer stage under `BoundedCapture` with a
consumer stage that echoes what it read; compare against the unbounded
producer's output. Artefact: `tests/behaviour/test_bounded_capture.py`
(scenario "Bounded capture does not bound the downstream stage") and
`tests/features/bounded_capture.feature`. Evidence: the consumer's captured
output is the full producer payload while the producer's own `stdout` is the
bounded tail. Non-vacuity: the payload is strictly larger than the bound, so a
leaking bound changes the consumer's output.

**O7 — Per-stream independence.** *Discharged at M1.* Invariant:
`resolved_capture` resolves each stream from its own field, inheriting
`capture` only when that field is `None`, and never mixing the two. Method:
property test over `capture × capture_stdout × capture_stderr` with Hypothesis,
plus parameterized cases for the shorthand/override matrix, plus explicit
rejection cases for invalid values. Artefact:
`cuprum/unittests/test_capture_policy.py` (the plan named
`test_capture_options.py`; the file that exists is named for the module it
covers). Evidence: **measured.** Mutating `RunOutputOptions.__post_init__` so
both streams resolve from `capture_stdout` — the joint switch this obligation
exists to exclude — produced 5 failures:
`AssertionError: stderr resolved to FullCapture() for declared False` plus four
DID-NOT-RAISE cases in `test_capture_resolution_matrix`, which lost its
`override-off` and `override-bounded-over-off` rows because the mutation made
stderr follow stdout into a capture that had been switched off. Restoring the
file returned the suite to 30 passed. Non-vacuity: the generated space includes
`None` and non-`None` values for both overrides and both booleans for
`capture`, so all four resolution quadrants are reached; the mutation above is
the seeded fault and it is rejected for the intended reason rather than by an
unrelated error.

**O8 — Partial output survives failure.** Invariant: a bounded or spilled
stream that is cut short by a timeout still reports the bytes received up to
that moment (text for the bounded case, a readable spill file for the
file-backed case). Method: parameterized test with a child that writes a known
prefix then stalls past a short timeout, asserting the retained payload.
Artefact: `cuprum/unittests/test_capture_timeout.py`. Evidence: the assertion
fails if finalization only runs on the clean-EOF path. Non-vacuity: the child
writes a non-empty prefix before stalling, and the test asserts that prefix
specifically rather than mere non-emptiness.

**O9 — The default is unchanged.** *Partly discharged at M1; the runtime half
closes at M3.* Invariant: `RunOutputOptions()` resolves both streams to
`FullCapture()` and `CommandResult`'s seven positional slots are unchanged, with
`captures` defaulting to `()`. Method: parameterized assertions in the
public-API pinning test. Artefact: `cuprum/unittests/test_public_api.py`.
Evidence: **measured at M1** for the declaration and resolution halves —
`test_public_api.py` asserts `fields[-1] == "captures"` and
`positional[-1] == "relay_fallbacks"` in the same test, so the two cannot be
satisfied by a field that takes a positional slot, and the type-hint assertion
`hints["captures"] == tuple[CapturedStream, ...]` proves the annotation
resolves at runtime rather than being a string. The resolution half is measured
by `test_capture_resolution_matrix`'s `inherit-true` and `inherit-false` rows.
Remaining at M3: that a run with no capture configured still produces empty
`captures` and complete `stdout`/`stderr` text end to end. Non-vacuity: the
test constructs a seven-positional `CommandResult` and reads the bound value
back, so a shifted slot is detected rather than assumed.

Axioms relied on (not verified here, treated as given): CPython's
`tempfile.NamedTemporaryFile` creates a file at the returned `name` and
`Path.unlink(missing_ok=True)` is idempotent; `bytearray` and `memoryview` have
the documented slicing semantics; `asyncio.shield` plus `asyncio.ensure_future`
give the teardown guarantee `_process_lifecycle._shielded_cleanup` already
relies on; the repository's existing drain tests correctly exercise the real
drain.

Residual gap: bounded-memory behaviour under a genuinely large stream is
verified by construction and by the below/above-threshold tests rather than by
a resident-memory measurement; a peak-RSS assertion would be load-sensitive and
is deliberately not planned.

## Plan of work

Four milestones, each a coherent plateau that ends green.

**EP-M1 — Vocabulary and public surface (Task 1).** Add
`cuprum/capture_policy.py` with the three frozen slotted policy dataclasses, the
`CapturePolicy` and `CaptureStreamName` type aliases, validation via
`object.__setattr__`-free `__post_init__` raising `ValueError` for non-positive
bounds, and a frozen slotted `CapturedStream` metadata record with a
`read_text()` accessor. Add `DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES` to
`cuprum/_constants.py`. Add `capture_stdout`/`capture_stderr` to
`RunOutputOptions` — declared `kw_only` so the pinned first six positional
slots do not move — resolve them from `capture` in `__post_init__`, and expose
`resolved_capture`. Add the trailing `kw_only` `captures` field to
`CommandResult` and a `captures` property to `PipelineResult`. Export the new
names from `cuprum/sh/__init__.py` and `cuprum/__init__.py`. Nothing consumes
the policies yet, so M1 is a pure addition and every existing test passes
unchanged.

**EP-M2 — Accumulators, events, and the drain tap (Task 2).** Add
`cuprum/_capture_sink.py` with the three accumulators behind one interface: a
full accumulator, a bounded-tail accumulator, and a spill accumulator, each
offering "absorb this chunk" and "finalize" (public value plus metadata), plus
an async cleanup for the spill file. Add `cuprum/capture_events.py` (closed
`StrEnum` vocabularies plus a bounded-field `CaptureEvent`) and
`cuprum/capture_observation.py` (a `ContextVar`-backed `observe_capture`
channel modelled on `cuprum/echo_observation.py`). Change
`_StreamConfig.capture_output` from `bool` to `CapturePolicy | None`, allocate
the accumulator in `_build_drain_state`, feed it in `_deliver_chunk`, and
finalize it in `_finish_drain`, emitting capture events on the way. Update
every direct `_StreamConfig` construction in the tests.

**EP-M3 — Threading, materialization, and teardown (Task 3).** Replace
`_SubprocessExecution.capture: bool` with the two resolved policies and pass
each to its own stream config, mirroring how `echo_output` already differs per
stream. Do the same for `_PipelineRunConfig`. Widen the capture plumbing types
from `str | None` to the finalize result without changing tuple alignment.
Materialize each finalize result into `stdout`/`stderr` plus a `captures` entry
at the result-construction sites. Register the accumulator cleanup through
`_shielded_cleanup` at `_reconcile_run_tasks` and the pipeline teardown. Handle
the timeout path so partial output is finalized, keep the cancellation path's
`CancelledError` re-raise unchanged, and make a spill storage failure non-fatal.

**EP-M4 — Tests and documentation (Task 4).** The unit tests, BDD feature and
behaviour tests, and the design, migration, and roadmap documentation updates
enumerated in `Verification plan`.

Test-first applies at the level of each milestone's new behaviour: for each
unit test that pins new behaviour, the test is written and run against the
unmodified code first (Red), the minimal production change makes it pass
(Green), and the surrounding code is then cleaned up with the test re-run
(Refactor). Where a test cannot be written before the module it imports exists
— the accumulator tests, which import `cuprum._capture_sink` — the module's
public interface is written first with a body that raises
`NotImplementedError`, the failing test is observed, and then the body is
filled in.

## Milestones and plateaus

**EP-M1 — Vocabulary and public surface.** Requirements and gaps:
`ISSUE-443-AC-per-stream` (vocabulary half), `ISSUE-443-AC-default-preserved`.
End state: the new types exist and are importable; `RunOutputOptions` resolves
them; `CommandResult` carries `captures`; nothing else has changed; all
existing gates pass. Acceptance evidence: `make test` passes with only the
`test_public_api.py` tail-pin change; a REPL shows
`RunOutputOptions(capture_stdout=BoundedCapture(max_bytes=8)).resolved_capture`
returning the policy for stdout and `FullCapture()` for stderr. Conformance
check:

- Requirements satisfied: vocabulary and resolution exist and are exported.
- Design still followed: fields are additive, defaults preserve behaviour.
- Upstream assumptions valid: no path or type in the source plan's Task 1
    contradicts the tree once the four corrected paths are used.
- No unapproved public interface change: additions only; no existing
    signature moved.
- No trust-boundary or persisted-format change: no files are written yet.
- Trace links current.
Recovery: revert the M1 commit; nothing depends on it yet. Remaining gaps: no
policy is enforced; nothing spills; no events are emitted. Compatibility
decision: `<none>` — pre-1.0 package, additive-only change.

**EP-M2 — Accumulators, events, and the drain tap.** Requirements and gaps:
`ISSUE-443-AC-bounded-capture`, `ISSUE-443-AC-unicode-boundary`. End state: the
drain honours a policy when one is supplied; bounded and spilled capture work
end to end at the drain level; the event channel emits. Acceptance evidence:
`cuprum/unittests/test_bounded_capture_drain.py` and
`test_capture_accumulator.py` pass; `make lint` and `make test` pass.
Conformance check:

- Requirements satisfied: O1, O2, O3, part of O4.
- Design still followed: the accumulator is a tap of `_deliver_chunk`; the
    relay path is untouched.
- Upstream assumptions valid: `_encode_prefix` is reusable as hoped (or the
    deviation is recorded).
- No unapproved public interface change: the accumulator modules and the event
    module are public-by-export only; the drain internals stay private.
- No trust-boundary or persisted-format change: spill files are new but are
    temporary by contract and removed deterministically.
- Trace links current.
Recovery: revert the M2 commit; M1's vocabulary is still coherent on its own.
Remaining gaps: nothing resolves a policy from `RunOutputOptions` into the
execution path yet, so the feature is not reachable from a public entry point.
Compatibility decision: `<none>`.

**EP-M3 — Threading, materialization, and teardown.** Requirements and gaps:
`ISSUE-443-AC-relay-untouched`, `ISSUE-443-AC-partial-on-failure`,
`ISSUE-443-AC-storage-failure`, `ISSUE-443-AC-deterministic-cleanup`. End state:
`run_sync(RunOutputOptions(capture_stdout=BoundedCapture(...)))` returns a
bounded `stdout` and matching `captures`; a file-backed run returns
`stdout is None` with a path; spill files are removed on every outcome.
Acceptance evidence: the end-to-end unit tests and the behavioural feature pass;
`make test` passes. Conformance check:

- Requirements satisfied: O4–O8.
- Design still followed: only the final stage captures stdout, as before;
    interior stages relay.
- Upstream assumptions valid: the reconciliation choke points are the right
    registration sites; verified by the cancellation and timeout tests.
- No unapproved public interface change: `stdout`/`stderr` stay
    `str | None`.
- No trust-boundary or persisted-format change: spill files are owned
    temporaries; the path is reported but the format is opaque bytes.
- Trace links current.
Recovery: revert the M3 commit; M1 and M2 remain coherent. Remaining gaps:
documentation not yet updated; no behavioural BDD coverage. Compatibility
decision: `<none>`.

**EP-M4 — Tests and documentation.** Requirements and gaps: every acceptance
criterion above, plus the documentation obligations. End state: the design
document, migration guide, and roadmap describe the new model; all gates pass;
the work is committed and pushed. Acceptance evidence:
`make check-fmt lint test markdownlint` all pass; `docs/cuprum-design.md` §7.4
no longer claims capture is a joint switch. Conformance check:

- Requirements satisfied: all traced acceptance items.
- Design still followed.
- Upstream assumptions valid.
- No unapproved public interface or dependency change.
- No unapproved trust-boundary or persisted-format change.
- Trace links current.
Recovery: documentation-only revert is safe. Remaining gaps:
`BytesCommandResult` reports no capture metadata, so byte mode cannot flag a
truncation or a spill. Compatibility decision: `<none>`.

## Concrete steps

All commands run from the repository root — the working directory this plan is
checked out in.

Every `pytest` invocation goes through `uv run`, which is what the Makefile's
own `PYTEST = $(UV_RUN_ENV) uv run pytest` does; the bare system interpreter
has no `pytest` installed and `python -m pytest` fails with
`No module named pytest`. The system interpreter is also 3.12.14 while `uv run`
provisions 3.14.7, so the tests must be run the way CI runs them.

Red, for a focused unit test:

```sh
uv run pytest cuprum/unittests/test_capture_accumulator.py -x -q 2>&1 | tee /tmp/test-issue-443-$(git branch --show-current).out
```

Green, and the wider unit suite:

```sh
uv run pytest cuprum/unittests -q 2>&1 | tee /tmp/test-issue-443-$(git branch --show-current).out
```

Behavioural:

```sh
uv run pytest tests/behaviour/test_bounded_capture.py -q 2>&1 | tee /tmp/test-issue-443-$(git branch --show-current).out
```

Commit gates, run strictly sequentially and delegated to `scrutineer`:

```sh
make check-fmt
make lint
make typecheck
make test
make markdownlint
```

Spelling and Mermaid, for the documentation-only changes:

```sh
make spelling
make nixie
```

## Validation and acceptance

Acceptance is behavioural. After EP-M3, the following script must print the
bounded tail, the exact dropped count, and a removed spill path:

```python
import pathlib
from cuprum import (
    BoundedCapture,
    FileBackedCapture,
    RunOutputOptions,
    scoped,
    ScopeConfig,
)

# ... build a command that writes 16 ASCII bytes to stdout and 4096 to stderr
result = cmd.run_sync(
    output=RunOutputOptions(
        capture_stdout=BoundedCapture(max_bytes=8),
        capture_stderr=FileBackedCapture(spill_threshold_bytes=1024),
    )
)
assert result.stdout == "efghijkl"
assert result.captures[0].dropped_bytes == 8
assert result.captures[1].storage == "file"
assert result.captures[1].path is not None
assert not pathlib.Path(result.captures[1].path).exists()
```

and the relay acceptance, in a two-stage pipeline whose producer is bounded,
must show the consumer receiving the producer's complete output.

Quality criteria:

- Tests: `make test` passes, including the new unit and behavioural suites.
- Verification: O1–O9 discharged as described in `Verification plan`.
- Lint/typecheck: `make lint` and `make typecheck` pass with no new
  suppressions.
- Performance: no benchmark target; the change must not add work to the relay
  path, which the untouched-relay tests demonstrate structurally.
- Security: no new dependency, no new network or filesystem trust boundary
  beyond a temporary file in a caller-controlled directory; the spill path is
  reported to the caller and removed deterministically.

Quality method: the repository's own gates, run sequentially, plus a CodeRabbit
review after each milestone.

## Idempotence and recovery

Every step is a source edit or a test run and is safe to repeat. Spill files
are created under `tempfile.gettempdir()` (or a caller-supplied directory) with
a random name and removed by the owning run; a run killed with an uncatchable
signal may leave one behind, which is the same guarantee `tempfile` gives any
caller. Reverting a milestone is `git revert` of that milestone's commit;
because each milestone is additive and leaves the tree green, reverting the
latest one never invalidates an earlier one.

## Artefacts and notes

To be filled in as milestones complete. Expected evidence includes the
transcript of the failing accumulator test before the boundary walk is wired
in, and the failing spill-cleanup assertion under cancellation before
`_shielded_cleanup` registration.

## Interfaces and dependencies

No new third-party dependency. Standard library only: `dataclasses`, `enum`,
`contextvars`, `pathlib`, `tempfile`, `os`, `typing`.

In `cuprum/capture_policy.py` — the vocabulary the caller names:

```python
type CapturePolicy = FullCapture | BoundedCapture | FileBackedCapture
type CaptureStreamName = typ.Literal["stdout", "stderr"]
type CaptureStorage = typ.Literal["memory", "file"]


@dc.dataclass(frozen=True, slots=True)
class FullCapture: ...


@dc.dataclass(frozen=True, slots=True)
class BoundedCapture:
    max_bytes: int


@dc.dataclass(frozen=True, slots=True)
class FileBackedCapture:
    spill_threshold_bytes: int | None = DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES
    # Normalized to ``Path`` in ``__post_init__``, but annotated wide so a
    # caller may spell the directory as either.
    directory: pathlib.Path | str | None = None
    keep_file: bool = False


@dc.dataclass(frozen=True, slots=True)
class CapturedStream:
    stream: CaptureStreamName
    policy: CapturePolicyKind
    total_bytes: int
    dropped_bytes: int = 0
    storage: CaptureStorage = "memory"
    path: pathlib.Path | None = None
    owned: bool = False
    storage_failed: bool = False

    @property
    def truncated(self) -> bool: ...

    def read_text(self, *, encoding: str = "utf-8", errors: str = "replace") -> str: ...
```

The implementation is split across four modules rather than the one
`_capture_sink.py` this plan first sketched:

- `cuprum/_capture_types.py` — the `CaptureSink` protocol (`absorb`,
  `finalize`, `async cleanup`), `CaptureSinkRegistry`, `CaptureFinal`, and the
  idempotent `SpillHandle`.
- `cuprum/_capture_accumulators.py` — the in-memory `_FullAccumulator` and
  `_BoundedAccumulator`, plus `build_accumulator(policy, *, stream, settings)`
  returning `None` when the stream is not captured.
- `cuprum/_capture_file.py` — `_FileAccumulator`, the only accumulator that
  owns a file and so the only one with a storage obligation. It carries its own
  module because absorbing a create, write, flush, or read failure is most of
  what it does.
- `cuprum/_capture_cleanup.py` — `_cleanup_capture_sinks` and
  `_finalize_capture_handles`, the removal paths shared by single-command,
  pipeline, and `lines()` runs.

In `cuprum/capture_events.py`, define `CaptureStreamName` (re-used),
`CaptureEventCategory(enum.StrEnum)` with `TRUNCATED`, `SPILLED_TO_FILE`,
`STORAGE_WRITE_FAILED`, `STORAGE_FLUSH_FAILED`, and `STORAGE_READ_FAILED` — the
last added because byte mode reads a spilled file back during result
construction, and a failure there is absorbed like any other storage call — and:

```python
@dc.dataclass(frozen=True, slots=True)
class CaptureEvent:
    stream: CaptureStreamName
    category: CaptureEventCategory
    total_bytes: int | None = None
    dropped_bytes: int | None = None
    spilled_bytes: int | None = None
    error_type: str | None = None
```

In `cuprum/capture_observation.py`, mirror `cuprum/echo_observation.py`:
`observe_capture(hook) -> CaptureHookRegistration`, `current_capture_hooks()`
and a private `_emit_capture_event(event)`.
