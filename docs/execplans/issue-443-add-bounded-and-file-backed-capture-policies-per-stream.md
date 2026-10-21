# Add bounded and file-backed capture policies per stream

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: COMPLETE

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
3. Spill a stream to a temporary file once it exceeds a threshold, with the
   path reported on the record so the caller can see where the spill went. The
   file is owned and deterministically removed by the library unless
   `keep_file=True`, which is the only setting that leaves it in place for the
   caller to read after the run; otherwise the reported path records where the
   stream was spilled, and no longer resolves once the run has settled.
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
- **`stdout`/`stderr` stay `str | None`.** The public type contract is not
  widened to a union with a handle type, and a file-backed stream puts its path
  in `captures` rather than in `stdout`/`stderr`. `None` there means this
  stream's payload did not survive as text: a file-backed stream reports `None`
  only once it has actually spilled, since below the threshold it is still
  buffered on the heap and returns its complete text. A spill whose file could
  not be created is the other `None`-free case — it stays on the heap, so the
  text is returned and the record reports `storage="memory"` beside
  `storage_failed=True`.
- **Byte-exact mode reports the same capture metadata, but reads a spill back.**
  `BytesCommandResult` carries the same keyword-only `captures` tuple as
  `CommandResult`, and `BytesPipelineResult.captures` reads the final stage, so
  a `run_bytes()` caller can tell a bounded tail from a complete stream, name a
  spill, and see a storage failure. It matters most here: the bytes alone
  cannot distinguish a complete capture from a shortened one. The byte-mode
  payload includes every byte accepted by the accumulator only if
  finalization's flush and read-back both succeed. After an append failure,
  finalization still attempts the flush; if it fails, bytes still buffered are
  not guaranteed in the payload, and a read-back failure returns `None`. That
  read is O(stream size), so byte mode gets none of the bounded-memory
  guarantee that text mode gets.
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
  `test_bounded_capture_properties.py`, `test_bounded_capture_seam.py`,
  `test_bounded_capture_edges.py`, `tests/behaviour/test_bounded_capture.py`
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
  deletion and moving the branch's `_reconcile_shielded` helper beside the
  split post-exit module, so no duplicate definition survives; the migration
  guide's capture section was appended to the 0.2.0 feature list rather than
  dropped by the section renumbering; and the `pyproject.toml` dead-code entry
  points merged entry-by-entry. Backup branch
  `backup/issue443-pre-rebase-20261017` retains the pre-rebase tip.
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
- [x] (2026-10-17) Clear the eleven findings from the second CodeRabbit
  review, then rebase onto `origin/main` (`304779c25`). Six findings were
  production changes: a `lines()` completion path that never closed its spill
  handle; two pipeline failure paths that drained observers and released sinks
  as two separately shielded steps; a spawn-failure path with the same split; a
  per-chunk flush on the event loop, replaced by one flush in
  `_finalize_spilled` (with `_flushed_bytes`, so a failed flush reports the
  bytes that reached the file); and `RunOutputOptions` writing its resolution
  back over the init fields, which made
  `dataclasses.replace(..., capture=False)` silently keep capturing. The
  remaining five were test hygiene (assert messages, spill-handle close-out, a
  wrongly worded predicate message, a stream name that described the wrong
  stream) and an ExecPlan interface section that still described the single
  `_capture_sink.py` module the split replaced. The `lines()` fix and the
  `replace` fix were both mutation-measured: removing the call, and restoring
  the write-back respectively, each fail the new tests. The rebase replayed
  thirty commits; the only conflict was `pyproject.toml`, where `main`'s
  three-line build-backend comment above `[build-system]` was inserted beneath
  this branch's Skylos and duplication-gate entries. The full gate set was
  green at `c42e57292` — pytest `3509 passed, 93 skipped`, nextest
  `167 tests run: 167 passed (2 slow), 0 skipped`, doctests
  `0 failed, 3 ignored` — and the plan and PR-body updates that follow it are
  documentation-only, so the gates are re-run once more at the documentation
  commit before the review is requested.
- [x] (2026-10-17) Clear the fifteen findings from the fourth CodeRabbit
  review, run against the green tree at `c616b6649`. Three were production
  changes: the `_discard_drain` sink leak, `finalize` mutating `_dropped` and
  re-emitting `TRUNCATED`, and the mark-less decode a bound could force on
  `utf-16`, `utf-32`, or `utf-8-sig`. Each has a regression witness measured
  under mutation; the details and the one vacuous first attempt are in
  `Surprises & discoveries`. The rest were test hygiene (assert messages in
  `test_capture_cleanup.py`, `test_capture_spill_cleanup.py`,
  `test_public_api.py`, `test_line_stream_exit.py`, and the stderr-length
  assertion in `tests/behaviour/test_bounded_capture.py`), a duplicated
  `CapturePolicyKind` public-surface test that was deleted, three documentation
  corrections (`docs/users-guide.md` twice and
  `docs/v0-2-0-migration-guide.md`), an ExecPlan section that had drifted from
  the module split, and `typos.toml`, skipped with the reason recorded in
  `Surprises & discoveries`.
- [x] (2026-10-18) Clear the four findings from the fifth CodeRabbit review.
  One was a real behavioural defect introduced by the round-4 BOM fix:
  `_bounded_settings` was called on every finalization, so a bounded capture of
  a stream it *fits* decoded under the concrete codec and left the mark in the
  payload as a leading U+FEFF, where a full capture of the same bytes reports
  plain text. It now resolves only once the bound has dropped something.
  Measured across the three families before editing (`'﻿abc'` for all of
  `utf-16`, `utf-32`, and `utf-8-sig`); the new regression case fails all three
  parametrizations under mutation. The other three were a module docstring that
  generalized one scenario's property to all of them, a test parameter
  annotated narrower than its rows, and an ExecPlan conformance source naming a
  `/tmp` file no reader can open.
- [x] (2026-10-18) Clear the sixth CodeRabbit review's findings. The major one
  was a gap rather than a defect: `BytesCommandResult` had no `captures` field,
  so a `run_bytes()` caller could not learn that a policy bounded, spilled, or
  failed to retain a stream — exactly the case where the payload cannot say so
  itself. Both byte result classes now carry the records, filled from the same
  drain data as the text classes, and `BytesPipelineResult.captures` reads the
  final stage. The new `test_byte_capture_records.py` fails all four byte cases
  when either construction site omits the records. The rest were the four
  missing `__all__` entries for the capture-event surface, tests for the byte
  records, the users' and migration guides now stating that byte results carry
  `captures`, the ExecPlan's three stale "byte mode reports no metadata" claims,
  `typ.Any` casts replaced with `CaptureStreamName`, bare asserts given
  messages, and the generated-input classes published through
  `hypothesis.event` beside the deterministic corpus counts.
- [x] (2026-10-18) Clear the round-8 findings from the local
  `coderabbit review --agent` pass. Six needed code or test changes: a bare
  `return` removed from a pump double (pylint's enabled `useless-return` would
  have caught it at the next full `make lint`); `test_pipeline_config.py` now
  asserts the per-stream policies the config actually carries instead of merely
  that construction succeeded; the `_Retained` record (since moved to
  `_bounded_capture_support.py`) makes the suffix property measure the
  accumulator's own payload rather than a slice of the input; the two
  timeout-cleanup tests enter the capture observer before the deadline can
  fire, so their witness covers the child's whole life; `cuprum.sh.output`
  publishes `IOOptions` again through a lazy module `__getattr__`, after the
  2026-10-14 move silently dropped the public path; and the stdio redirection
  guard reads the resolved per-stream policy instead of the `capture`
  shorthand. The `IOOptions` export re-took the 400-line ceiling and forced the
  `_resolved_options.py` mixin split (see `Decision log` and the ADR-007
  addendum). Two findings were reasoned skips: the release gate's `_drain`
  really does return `(None, None)` on the discarding route, and the
  `docs/developers-guide.md` line it cited describes that correctly. Verified
  with `uv run ruff check` (clean), `uv run ruff format --check` (clean), pinned
  `ty==0.0.74` (clean), pinned `pylint==4.0.9` (10.00/10 on both modules), and
  `pytest` over the five affected test modules (`74 passed`). Date/Author:
  2026-10-18, implementation agent.
- [x] (2026-10-18) Close the round-8 finding against the trim's
  unrepairable-codec gap, and record the corrected scope in prose. The report
  was concrete: for Shift-JIS, `b"\x81a"` decodes to `"‖"`, so with
  `max_bytes=1` the trim leaves the orphaned trail byte and `_render` returns
  `"a"` — a character the child never wrote. Took the report's second option
  and refuse before spawning rather than tracking decoder state, because a
  stateful accumulator would have to keep the *whole* stream to resolve a
  boundary, which is the memory the bound exists to avoid. The classifier in
  the new `cuprum/_bounded_encoding.py` calls a codec trim-safe when it is a
  repairable name (UTF-8, the fixed-width UTF-16/UTF-32 names, `utf-8-sig`) or
  when a probe shows it is single-byte; the probe feeds every one of the 256
  byte values to a fresh incremental decoder and asks `getstate()` what it is
  holding, which is a definition rather than a sample. Verified exact across
  all 98 registry names: 59 single-byte accepted, 7 UTF names accepted, 32
  multi-byte/stateful refused, 7 transform codecs refused. The escape families
  are the reason the probe is a pending-byte question: `hz` and `iso2022_*`
  decode every lone byte as text, so a decoded-length comparison admits them.
  Wired into both pre-spawn seams — `_build_subprocess_execution` for the
  single-command paths and `_prepare_pipeline_config` for both pipeline entry
  points — because a rule only one seam consults is a rule the others flout.
  Witnesses measured under mutation, each reverted with `cp`: disabling the
  guard body fails 5 cases, the command seam 2, the pipeline seam 1, and the
  pending-byte probe 2 (`hz` and `iso2022_jp`). Verified with
  `uv run ruff check` and `ruff format --check` (both clean), pinned
  `ty==0.0.74` (clean), pinned `pylint==4.0.9` (10.00/10 on all four modules),
  48 new unit cases, and 234 cases across the capture-related unit and
  behaviour modules. No `tests/features/` scenario was added: the pipeline seam
  is already exercised through the public `run_sync` on a real two-stage
  pipeline in the unit module, so a BDD row would restate the same run rather
  than add a distinction. Date/Author: 2026-10-18, implementation agent.
- [x] (2026-10-18) Take the round-8 spill I/O off the event loop. The two
  findings against `cuprum/_capture_file.py` are one change: create, append,
  flush, and byte-mode read-back were synchronous calls on the loop that also
  drives the child's other stream, the deadline, and cancellation. A new leaf
  module, `cuprum/_capture_spill_io.py`, holds `_create_spill` — the one call
  that must stay a named function, because the failure tests patch it — and
  `_SpillIo`, which runs each blocking call on a worker thread under a
  per-accumulator `asyncio.Lock`; every other call is a bound method supplied
  at the call site, because the first shape's one-line forwarding functions
  tripped the tree's `R9104` trivial-attribute-wrapper rule.
  `asyncio.to_thread` alone starts a separate worker per call, so the file's
  contents would otherwise follow executor-scheduling order rather than the
  order the child wrote. Every call is shielded through the existing
  `_shielded_cleanup`, so a cancelled drain waits for an in-flight write before
  teardown unlinks the file underneath it. `CaptureSink.absorb` and `finalize`
  are now coroutines on all three accumulators, so the drain awaits one
  protocol rather than testing which policy it holds; the two heap-backed
  accumulators answer without suspending. `SpillHandle.close` closes its
  descriptor on a worker for the same reason, `keep_file=True` excepted as
  before. The leaf's scope, the import direction that keeps it acyclic, and the
  protocol change are recorded in the ADR-007 addendum of 2026-10-18 and
  referenced from the design doc's capture section. Five test modules moved to
  the async seam; the two that arm a simulated filesystem failure between two
  awaits keep the phases inside one `asyncio.run`, because splitting them would
  let a sink pass here that cannot finish a stream in a real run. Evidence: 186
  passed across the five converted modules, 8 passed in
  `test_capture_spill_cleanup.py`, 11 passed in the two modules whose stub
  sinks also grew the async seam, and 1159 passed, 68 skipped, 2730 deselected
  across
  `cuprum/unittests/ -k "capture or spill or drain or stream or pipeline or
  line_stream"`
  (log: `/tmp/converted-capture-<branch>.out` and the broad sweep it followed).
  `ruff check`, `ruff format --check`, and `make markdownlint` (with
  `make spelling`) are clean; `make nixie` reports all diagrams valid.
  Date/Author: 2026-10-18, implementation agent.
- [x] (2026-10-18) Clear the three findings the gates raised after the round-8
  fixes. The five one-line spill thunks were rejected as `R9104`
  trivial-attribute wrappers by the DF12 pass; they are deleted and the
  accumulator passes `file.write`, `file.flush`, `file.close`,
  `path.read_bytes`, and `Path.unlink` to `_SpillIo.run` at each call site,
  which is the plugin's documented remedy and leaves only `_create_spill` as a
  named function because the failure tests patch it. `C9107` rejected the
  `ty: ignore` in `resolved_echo` because the existing explanation sat four
  lines above the pragma; the cast moved up and the comment now sits
  immediately before the line it explains. The developers' guide's
  `(None, None)` sentence was corrected to say that a discarding drain reports
  the capture-*policy* pair `(None, None)` and settles each stream as an empty
  `_CapturedDrain`. `mdtablefix --wrap` then rejected the reflowed prose in the
  ADR-007 addendum and this plan; the paragraphs are reflowed with no words
  changed. Evidence: all seven gates exit 0 at `fcbd5b258` with a clean tree
  before and after each — `make check-fmt`, `make typecheck`, `make lint`,
  `make markdownlint`, `make nixie`, `make duplication` ("duplication gate
  passed; 66 allowed by reasoned exceptions"), `make test` (Python 3864 passed
  / 93 skipped plus 843 and the selector suites; Rust nextest 167/167; doctests
  ok). The log names below are the run3 runner's fixed prefix collapsed, so `…`
  stands for `issue-443-20261018`: `gate-check-fmt-…-run3-…T124130Z.out`,
  `gate-typecheck-…-run3-…T124130Z.out`,
  `gate-lint-…-scrutineer3-…T125822Z.out`,
  `gate-markdownlint-…-scrutineer3-…T130738Z.out`,
  `gate-nixie-…-scrutineer3-…T130809Z.out`,
  `gate-duplication-…-scrutineer3-…T130817Z.out`, and
  `gate-test-…-scrutineer3-…T130823Z.out`, all under `/tmp`. Date/Author:
  2026-10-18, implementation agent.
- [x] (2026-10-18) Clear the eight findings of review round 9, in seven
  commits (`03320f8d2`, `c9766933d`, `559f2b582`, `73e35f22b`, `8e0fad76f`,
  `83b94672b`, `aca0825d3`) on top of `cd3a35866`. The one production defect
  was a real orphan window: `_FileAccumulator._spill` published the
  `SpillHandle` only after the prefix was written and flushed, so a
  cancellation between the `delete=False` create and that point left a file no
  result named and no sink could reach. The handle is now published
  immediately, carrying `keep=False` until the prefix lands, and then promoted
  to the caller's keep policy; the regression test blocks the first write,
  cancels the absorb, and asserts teardown removes the file — deleting the
  publication lines makes it fail with the file still present. The rest were
  witness and accuracy repairs: `SpillHandle.close` now closes its file on a
  worker rather than the unwinding loop; the spill leaf binds
  `NamedTemporaryFile` by name so the failure tests substitute this module's
  dependency instead of rewriting `tempfile`'s global attribute for every
  importer; the bounded-encoding guard gained the `run` and `lines` seams its
  docstring already claimed; the byte pipeline case now pins both final-stage
  records with their exact policies and drop counts rather than "some record
  dropped bytes"; `cuprum/sh/output.py` names `pipeline_output`, not
  `_deprecated`, as the home of the flat keyword handling; and the ADR-007
  addendum's unsupported quotation and stale patch target are corrected in
  place (branch-added text, absent from the base, so nothing published is
  rewritten). Locally verified before the gates: `ruff check` and
  `ruff format --check` clean on all eight touched Python files, and 66, 17,
  45, 55, and 227 cases green across the five affected modules. Date/Author:
  2026-10-18, implementation agent.
- [x] (2026-10-18) Clear the round-10 findings, in five commits
  (`ae6f4ac32`, `94184c222`, `e15c01572`, `5d5e1b417`, `4da16c9fc`). Two were
  production defects. The create-then-publish window was still open: round 9
  published the handle before the prefix *write*, but a cancellation arriving
  during the create itself left a `delete=False` file nobody held, because the
  publish was a separate `await` that the cancellation reached first. The
  create and the publish are now one shielded step — `_create_published_spill`,
  built on `_run_shielded_step` so the file value survives a cancellation — and
  *adopt* runs before the cancellation is re-raised; the new `_BlockingCreate`
  regression blocks the worker inside the create and fails with the file still
  on disk if the adopt is moved after the raise. The second defect was that the
  two byte-mode bound cases in `test_bounded_capture_edges.py` never crossed
  their bounds, so their truncation assertions could not have failed. Their
  payloads are now chosen to exceed the bound. The two drain call sites were
  deduplicated onto `_drain_context`, the five capture-policy timeout cases
  moved to the readiness-marker pattern the other timeout suite already used
  (the helper is now in `tests/helpers/timeouts.py` so both share one
  implementation), and five documentation claims were corrected against the
  code: the execplan's `(0, n]` interval for the retained suffix, the
  users'-guide and roadmap promise that a bound never splits a code point (true
  for text, false for byte-exact), the developers'-guide claim that one owner
  awaits `absorb`, `finalize`, and `cleanup` in order, and the ADR-007
  addendum's wording that said the round-8 review *made* the calls blocking
  rather than identified them as such. Date/Author: 2026-10-18, implementation
  agent.
- [x] (2026-10-18) Split `cuprum/unittests/test_bounded_capture_drain.py` — 789
  lines when the round-10 review measured it, 754 at the split point once the
  shared drain drivers had moved to `_capture_sink_support.py` — at the seam
  the review named, into four modules: `_bounded_capture_support.py` (the shared
  `_config`, `_sink`, and `_Retained` fixtures),
  `test_bounded_capture_properties.py` (the two Hypothesis properties and the
  two corpus tests that keep their classes reachable),
  `test_bounded_capture_seam.py` (the config-ownership cases and the four cases
  that read a payload through `_drain` itself), and
  `test_bounded_capture_edges.py` (each bound boundary and the byte-mode
  cases). The `_feed`/`_absorb`/`_finalize` helpers had already moved to
  `_capture_sink_support.py`, so the split added no duplicated fixture. The
  test-function-name set is identical to the deleted module's and the same 28
  cases pass, so the split is behaviour-preserving rather than merely
  count-preserving. Date/Author: 2026-10-18, implementation agent.
- [x] (2026-10-18) Clear the six findings of review round 11, in five commits
  (`a9d355339`, `c99c4fad5`, `008c46b15`, `697af96bb`, `3eba9cb01`). One was a
  production defect: a cancellation landing inside `capture.absorb` now ends
  the drain like a cancelled read does, because a file-backed absorb awaits a
  shielded worker write and the old loop only caught cancellation around
  `stream.read`; an escaped one skipped `_finish_drain`, and the consumer
  result was replaced with an empty capture reporting `total_bytes=0` against a
  spill file that held the bytes. The regression gates the append's worker
  write, cancels inside it, and asserts the partial record; deleting the new
  `except` makes it fail with `CancelledError`. The blocking spill file and the
  drain suite now share one double through
  `cuprum/unittests/_blocking_spill_file.py`, which gates named writes and
  reports which one is held, so the case can wait for the append rather than
  for "some write" — the first write is the spill transition, a later one is an
  append, and they are different windows. The other findings were the read-back
  recovery promise in the users' and migration guides (the file follows
  `keep_file`, not the failure), two test comments that misstated their own
  arithmetic (a UTF-16 surrogate pair is two two-byte units; the seam payload
  is twice the bound, not five times), and the duplicate spill witness, now
  extended with an optional directory so the cleanup suite can use the shared
  one. Finding 1 also asked for the execution plan's timeout obligation to be
  corrected; O5 already states that a `keep_file=False` run leaves no file at
  the reported path on every outcome, and finding 15 independently said to
  leave the plan unchanged, so it was verified and not edited. Locally verified
  before the gates: 184 cases across the seven capture-related modules, and
  `ruff check` / `ruff format --check` clean on every touched Python file.
  Date/Author: 2026-10-18, implementation agent.
- [x] (2026-10-18) Clear the four findings of review round 12 in one commit.
  Two were one claim stated in four places: `total_bytes` was described as what
  the child wrote, but a run whose reader stopped early — a timeout, say — has
  no way to count bytes the child wrote after the parent stopped reading. The
  users' guide table cell, the design document's metadata paragraph, the
  `CapturedStream.total_bytes` docstring, and `sh/results.py`'s `captures`
  docstring now say "what the drain received", and the design document adds the
  partial-run caveat that explains the distinction. The third was a real
  design-document error about byte order: it said a headless UTF-16/UTF-32 tail
  falls back to the platform's native order, but the accumulator retains the
  stream's first `_BOM_MAX_BYTES` bytes in `_prefix` and `_bounded_settings`
  passes them to `_mark_selected_codec` before `_native_codec` is consulted, so
  a mark the bound discarded still selects the order it announced. Confirmed by
  running the path: with `utf-16` configured and a big-endian mark in the
  prefix, the tail decodes as big-endian. Native order now applies only to a
  stream that carried no mark at all. The fourth was a vacuous assertion in
  `test_a_fixed_width_encoding_realigns_the_retained_tail`: `text.endswith("")`
  is true, so an accumulator that trimmed the whole tail away passed. It now
  pins `final.payload == "b文c"`, which the arithmetic supports — the payload
  is 10 bytes in both encodings, the bound leaves 7, the realignment drops one
  byte and keeps six. Verified non-vacuous by mutation: changing the expected
  value to `"文c"` failed both parametrizations with the real `"b文c"`; and the
  old form was separately confirmed to pass 16/16 while
  `"a中b文c".endswith("")` is `True`. The fourth finding's sibling sweep over
  "what the child wrote" found no further false claims: the remaining hits
  describe synthetic drains that did receive every byte, and the byte-unit
  claim in the `capture_policy` module docstring is about what a bound
  measures, not about what a partial run can count. Date/Author: 2026-10-18,
  implementation agent.
- [x] (2026-10-18) Clear the four findings of review round 13. One was a
  production defect: `_StreamConfig.capture_sink` is derived in
  `__post_init__`, but it took part in equality, hashing, and the
  representation. `dc.replace` rebuilds it, and the value comparison then
  depended on the policy — `None` and two `_FullAccumulator` objects compare by
  identity, `_BoundedAccumulator` compares by value — while a bounded
  accumulator is unhashable, so `hash(config)` raised only under
  `BoundedCapture`. The repr printed whatever the stream had retained. Verified
  by running each symptom before the fix, not by reading the review:
  full-config equality was `False`, bounded hashing raised `TypeError`, and the
  repr contained `_prefix`. The field is now `repr=False, compare=False`, as
  `_PipelineRunConfig.stream_config` already was, with its reasoning recorded
  beside it. Three regression cases in `test_bounded_capture_seam.py` fail
  under the mutation that removes those options and pass after it. The first
  attempt at the equality case was itself vacuous — it compared two configs
  whose accumulators happened to be equal by value — which the mutation run
  exposed; the case now diverts `build_accumulator` so the sinks are provably
  distinct, and it asserts that distinctness before asserting equality. The
  other three findings were documentation claims that had drifted from the
  tests: O6 described an interior producer's stdout as bounded when interior
  stdout has no record at all (the real assertion is
  `result.stdout == _ANNOUNCEMENT` plus `dropped_bytes == 0` on the reporter)
  and cited a scenario name that does not exist; O8 required a readable spill
  after a timeout without naming `keep_file=True`, which the test does use and
  the default removes; and the design document said each interior stage's
  stderr "is captured" without the qualifier that an uncaptured stream
  contributes no record. All three were checked against the feature file, the
  timeout test, and `_build_stage_result` before editing. Date/Author:
  2026-10-18, implementation agent.
- [x] (2026-10-19) Rebase the branch onto `origin/main` at `a40898789` so the
  pull request's merge ref stops conflicting and CI can start. The replay
  boundary is the merge base `d93df9563`, and `d93df9563..b25ea26ea` contains
  91 commits; a merge-tree probe predicted one conflict and the replay hit
  eight, because a probe of the final trees cannot see the intermediate states
  each commit passes through. Every conflict was in documentation the upstream
  redirect work had rewritten: `docs/users-guide.md` four times,
  `docs/v0-2-0-migration-guide.md` twice, and both sides' claims had to be read
  against the code rather than chosen by label. The resolved head is
  `41e13f7be`, plus one follow-up commit for two prose corrections the replay
  exposed. `git range-diff` reports eight commits whose patch text changed and
  83 that are patch-identical. Four are the maturin wheel snapshot picking up
  `cuprum/redaction.py` from the new base, and one is a table ordinal that
  follows the new base's own numbering. The other three are the two
  documentation commits above and the lint commit that shares their rewrapped
  prose. Two things the replay taught: the branch's table ordinals (`_Table 4`
  for observation channels) only agreed with the old base, and the upstream
  commit that rewrote the redirect section landed on a different part of the
  file than the branch's capture section, so the two had to be woven by hand
  rather than by taking either side. Date/Author: 2026-10-19, implementation
  agent.
- [x] (2026-10-19) Clear the ten findings of review round four in one commit.
  Six were documentation or docstrings: the `PipelineResult.captures` and
  `BytesPipelineResult.captures` docstrings denied that an interior stage's
  stderr is captured at all, which `_create_stage_capture_tasks` and
  `_build_stage_result` contradict; three prose corrections and a changelog
  sentence split followed. Four were test-strength corrections, each a case
  that could pass for the wrong reason: the discard-drain release case held a
  sink only in the stdout slot, the two refused-unlink cases could not tell an
  absorbed refusal from a cleanup that never tried, the shared-directory case
  could not see a "remove everything" cleanup, and the private `_path` reads
  pinned an internal field name. The `typ.cast("typ.Any", ...)` wrappers around
  two `monkeypatch.setattr` values and the two `isinstance()` splits in the
  policy tests were mechanical. A probe that narrowed the discard-drain release
  to `captures[:1]` fails the new stderr assertion, which is what establishes
  the strengthened case is not vacuous. The first certification run then failed
  `make lint` with `C0302` because the added docstring text took
  `cuprum/sh/results.py` to 401 lines; tightening the two `captures` docstrings
  brought it to 398 and the unpushed commit was amended, so every gate result
  recorded before the amend certifies an orphaned commit and was discarded. The
  next certification run then failed `make lint` on the two `match` statements
  this round added: DF12 `match-class-bind-self` (R1905) wants
  `case bool() as enabled:` and `case str() as name:` rather than a class
  pattern with a same-named positional capture. `5bf5d9325` restores those
  spellings; the forms match the same values. Date/Author: 2026-10-19,
  implementation agent.
- [x] (2026-10-19) Clear the four findings of review round five in four
  commits, after checking each against the code. Three were trivial prose
  corrections and one was structural:

  - `39ba953e3` corrects the rationale for resolving the `utf-16`/`utf-32`
    family names, in both docstrings and in `docs/cuprum-design.md`. The
    finding was right and the old text was wrong: a one-shot
    `b"a\x00".decode("utf-16")` returns `"a"` on this little-endian host, so
    the family decoders do *not* reject a headless stream. Probing further
    showed the claim is true only of the path the retained tail actually
    takes — the incremental decoder `_pending_bytes` consults raises
    `UnicodeDecodeError ... Stream does not start with BOM` for a first chunk
    with no mark, in both `strict` and `replace` modes. The rewritten text
    distinguishes a whole-stream decode from the tail path rather than simply
    taking either side. The new reason the resolution stands is the one the
    finding gives: a dropped mark that selected the non-native order would
    otherwise be decoded in native order and produce wrong text.
  - `0eb210c38` states the duplication family's membership and idiom in
    `pyproject.toml` without narrating the rebase. `make duplication` passes
    with 68 reasoned exceptions afterwards.
  - `09cf85901` states the same ownership in the ADR-007 branch-added addendum
    without referring to `main` or to a branch in flight. The finding also
    asked for an "empty line artefact" to be removed from that section; no
    such artefact exists. `git diff --check` exits 0 and a scan of lines
    1258–1300 finds no consecutive blank lines, so only the ownership rewrite
    was actionable.
  - `2bc21c202` splits `test_capture_spill.py` (533 lines) into itself (337)
    and `test_capture_spill_cancellation.py` (175), moving the two
    cancellation drivers and their cases rather than copying them, and lifts
    `_spilling_to`/`_spill` into `_capture_sink_support.py` (132 lines).
    Although the 400-line ceiling binds production only — test targets run
    pylint with `--disable=too-many-lines`, and `origin/main` itself carries
    test modules up to 959 lines — the split is still worth having. Both
    moved cases are still collected; 39 tests pass across the four spill
    suites and `4022 tests collected` across `cuprum/unittests`.

  Date/Author: 2026-10-19, implementation agent.
- [x] (2026-10-19) Replayed the 106 child-owned commits a second time, onto
  `f359f2ec4`, because the first replay targeted `d93df9563` and upstream had
  moved nine commits further on. The new head is `75ee5826e`; the replay itself
  ends at `ce446cf5d`, and the two commits above it reconcile the result class
  with the per-stream policies (see "Second replay onto upstream main" below).
  The audit is recorded there: six range-diff entries differ, five only in hunk
  context and one, the byte-result commit, at the `_build_command_result` seam
  where the child predicate was reconciled.

  Date/Author: 2026-10-19, implementation agent.
- [x] (2026-10-19) Merged `origin/main` at `102951f48` (bounded output
  delivery, PR #622) into the branch and reconciled the result. Sixteen paths
  conflicted; the branch-authored ADR became ADR 026 so main's accepted ADR 025
  kept its number, `_pipeline_config` was split at the 400-line ceiling, and
  the four stale duplication entries were checked against source before
  removal. See "Third integration" below.
- [x] (2026-10-19) Ran the seven gates sequentially against the merged tree.
  The first run stopped at `check-fmt`, `duplication`, and `test`; commit
  `b85f36cdd` fixes all three, and the second run at frozen head
  `7cbb726fdb0b30a4822be2eafa8f13bfde3df9be` passes all seven (5425 Python
  tests, 167 nextest tests, three doctest binaries). See "Fourth integration"
  below, which records each gate's elapsed time and closing evidence.

  Date/Author: 2026-10-19, implementation agent.
- [x] (2026-10-20) Close the O5 kept-file coverage gap from the candidate
  completeness review. Add run-level tests for a kept spill after successful
  and non-zero completion, timeout, and external cancellation after promotion;
  assert the retained bytes at the public `SafeCmd.run()` boundary. The two
  spill suites share `_capture_spill_run_support.py`. The focused suites report
  `12 passed`; two lint findings in the new test code were corrected. The full
  certification for the source diff then ran sequentially from parent
  `54372b6c8` with dirty-diff SHA-256
  `c0d9134149f730ed0cb43e203f86a0a17fe6b2d349ee7a6138cd925affecedee`; commit
  `c0a08bd01d19c93e52eb93a6ed7cfede14e1bd7e` contains that same diff. Across 22
  pytest sessions, `make test` reported 5,518 passed and 114 skipped; Rust
  nextest passed 167 of 167 tests, and all doctest binaries succeeded (three
  ignored tests). All seven gate records have exit status zero and are under
  `/tmp/` with the
  `*-cuprum-rebase-issue443-onto-f359f2ec4-review-followup-2-20261020.out`
  filename pattern.

  A later sequential revalidation used that same source candidate plus the
  then-current plan update: HEAD `c0a08bd01d19c93e52eb93a6ed7cfede14e1bd7e`,
  tree `48b2eb80342aa978a44322b0bb5f1cf9fa18966b`, and dirty-diff SHA-256
  `0f65f109a603f640ce2d5feeb1812c91bed6799b8f7c175c5dc77609aa213238`. Six gates
  passed; `make test` exited 2 after Python reported 5,518 passed and 114
  skipped and Rust nextest passed 166 of 167 tests. The
  `cuprum-streams::compile_tests::transition_privacy` trybuild case timed out
  at 1,800.064 seconds while its nested Cargo work waited on the shared package
  cache lock and compiler admission. The nested build reported finishing after
  19m 28s, but nextest had already timed out, so this attempt is not passing
  test evidence. Its seven logs are under `/tmp/` with the
  `*-83d745e2-c310-4ba9-9a19-bd9e4e873ee5-rebase-issue443-onto-f359f2ec4-execplan-final-20261020.out`
  filename pattern.

  The append-failure qualification requested by the candidate assessment was
  already present in the users' guide and design document on this descendant of
  `f926d12cb`: finalization attempts a flush, byte-mode read-back returns
  accepted bytes only when both succeed, and bytes still buffered after a
  failed flush are not guaranteed in the payload. The summary above repeated
  the unconditional guarantee and is corrected here.

  Date/Author: 2026-10-20, implementation agent.

- [x] (2026-10-21) Re-ran the two Markdown gates against the plan revision
  that records the evidence above, so the documentation row of the pre-merge
  checks has a gate record of its own instead of resting on the
  source-certification run. `make markdownlint` (18 s) and `make nixie` (1 s)
  both exit zero, and the dirty-diff SHA-256
  `161442e406e05ce4b5af004b179c9b2da382bd94ef0d81246c8c9c5c2f848f97` is
  identical before and after, so neither gate straddled a content change. That
  content had already passed `make check-fmt`, `make lint`, and
  `make typecheck` in the `doc-corrected-final` run at the same SHA, so all
  five gates applicable to a plan-only change hold for one exact tree.

  Recording this paragraph changes the content again, so the five results
  certify the revision *before* this bullet, not the commit that contains it.
  That is the same limitation as any plan that documents its own verification:
  the source certification above remains the test evidence, and this run adds
  no test evidence and supersedes none. Logs:
  `/tmp/markdownlint-cuprum-rebase-issue443-onto-f359f2ec4-docfinal-20261021.out`
  and `/tmp/nixie-cuprum-rebase-issue443-onto-f359f2ec4-docfinal-20261021.out`.

  Date/Author: 2026-10-21, implementation agent.

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

- Observation (rebase acceptance, 2026-10-18): the replay is 38 commits onto
  `d93df9563`, and `git range-diff` matched all 38 one-to-one. Seven differ
  textually — 10, 11, 14, 19, 21, 22, 26, 29 — and every one was traced to a
  cause that is benign or deliberate, so none is a silent loss. The pattern in
  all seven is that `main` advanced a hunk the branch also touched, and the
  replay kept both sides where a textual diff shows only `main`'s.

  The two that look most like loss are worth recording. Commit 14's diff shows a
  `pyproject.toml` dead-code entry being replaced, but the replacement is
  `main`'s own pre-existing `StdioTarget.__post_init__` reason appearing as
  *context*, and this branch's added `cuprum.capture_policy._is_positive_int`
  entry is byte-identical before and after; nothing was dropped. Commit 21's
  diff removes two of `main`'s `nose` duplication entries —
  `_pipeline_finalize ~ _command_finalization` and
  `_command_internals ~ _subprocess_execution` — and that is correct rather
  than a lost suppression: the rebased modules no longer share the import
  blocks those entries described, so keeping them would have left stale
  suppressions that the gate itself reports.

  The whole-tree audit backs the same conclusion:
  `git diff --diff-filter=D d93df9563 HEAD` is empty, there are no conflict
  markers, and no weave advisory was emitted during the replay.

  One genuine defect did surface, and it is the rebase's most useful lesson.
  `main`'s `docs/developers-guide.md` described `_reconcile_shielded` as taking
  "the run's capture setting" and its call sites as passing `capture=True` or
  `capture=False`. This branch renamed that parameter to `preserve` and removed
  the `_SubprocessExecution.capture` attribute entirely, so the inherited prose
  named an argument and an attribute that no longer exist. The text is equally
  stale at the pre-rebase branch head, so the rebase surfaced a latent branch
  defect rather than creating one; three passages now describe the `preserve`
  flag and the per-stream policies it reads. The lesson: a rebase that keeps
  `main`'s documentation green can still keep `main`'s *claims*, and prose
  about a renamed parameter is invisible to every gate.

  Finally, gate evidence does not survive a rewrite. The certification logs on
  disk name `HEAD=6f47e18ed69e17914526122996f0962756af3bf7`, which is the
  seventh commit of the pre-rebase series and is not an ancestor of the rebased
  head; the most recent `make test` log names `c12f2aa52`, two commits behind.
  Both are true statements about trees that no longer exist. Every gate has to
  run again at the final head before the branch is offered for review, and the
  log header is what makes that checkable. Date/Author: 2026-10-18,
  implementation agent.

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

- Observation (review round 3, cleanup ordering): composing the observe-task
  drain and the capture release into one coroutine made them survive a
  cancellation together, but not a *drain failure*.
  `_drain_tasks_during_cleanup` raises a `BaseExceptionGroup` when an observe
  task fails, so the release that followed it was skipped on all three pipeline
  failure paths — the run-failure path, the timeout path, and the
  failed-assembly path — and the spill file leaked. Each release is now in a
  `finally`. `test_pipeline_cleanup_finally.py` fails a drain on each path and
  asserts the sink closed; removing the `finally` from the timeout path alone
  makes that case fail and leaves the other two passing, which is what shows
  the three cases are separately witnessed rather than one case counted three
  times. Date/Author: 2026-10-17, implementation agent.

- Observation (review round 3, documentation contradictions): three separate
  documents each stated a contract the code does not have. The users guide said
  a failed removal "is reported only as a `CaptureEvent`", but
  `CaptureEventCategory` has no removal category and the design document and
  migration guide both describe that route as silent. The `CapturedStream`
  docstring said `storage_failed` is "``False`` for every capture that did not
  spill", but a failed *create* leaves the stream on the heap with
  `storage_failed=True`. And the M1 section of this plan credited every policy
  with an "`object.__setattr__`-free `__post_init__`", when `FileBackedCapture`
  uses one to normalize a `str` directory. All three now say what the code
  does. Date/Author: 2026-10-17, implementation agent.

- Observation (review round 3, test documentation and messages): the byte-mode
  accumulator test claimed "Byte mode is trimmed too", contradicting both
  `_BoundedAccumulator.finalize` and the sibling test that pins the raw tail;
  its docstring now names the front-drop in `absorb` as the mechanism that
  holds the bound. Two test modules also carried bare assertions, which the
  path instructions forbid; every assertion in `test_capture_policy.py` and
  `test_capture_spill_failure.py` now names the contract it checks.
  Date/Author: 2026-10-17, implementation agent.

- Observation (review round 4, discard-drain sink leak): `_discard_drain` in
  `cuprum/_line_stream/coordinator.py` drains the consumers directly instead of
  through `_reconcile_run_tasks`, so the accumulator release that path owes was
  never made. A consumer cancelled mid-read never finalizes, the drain context
  asks for `(None, None)`, and the sink is then the only remaining reference to
  a spill file: nothing unlinking it survives the run. The release is now in a
  `finally` inside `_discard_drain`, so it also runs when the drain itself
  raises. The first attempt at a witness, an integration test that breaks out
  of iteration and calls `aclose()`, was vacuous: `aclose()` exits through
  `_cleanup_failed_line_stream_run` → `_reconcile_run_tasks`, which already
  releases the sinks, so `_discard_drain` is never reached on that route. It
  was deleted and replaced with
  `test_discard_drain_releases_the_capture_sinks`, which calls `_discard_drain`
  directly; reverting the `finally` fails that case and passes the other seven
  in the module. Date/Author: 2026-10-17, implementation agent.

- Observation (review round 4, `finalize` was a command):
  `_BoundedAccumulator.finalize` did
  `self._dropped += len(raw) - len(retained)`, so a second read of the same
  capture reported a larger `dropped_bytes` than the first — breaking
  `dropped + retained == total` for whichever record the caller actually held —
  and published a second `TRUNCATED` event for one run. The count is now
  computed locally and only the "already emitted" bit is stored.
  `test_a_repeated_finalize_reports_the_same_drop_count` fails all three of its
  parametrizations when the mutation is restored. Date/Author: 2026-10-17,
  implementation agent.

- Observation (review round 4, BOM discarded by a bound): a plain `utf-16`,
  `utf-32`, or `utf-8-sig` encoding selects its real codec from a leading byte
  order mark, so a bound that discards the mark left `_trim_to_code_point`
  decoding a mark-less tail under the family name, which raises `UnicodeError`.
  `_BoundedAccumulator` now keeps the stream's first `_BOM_MAX_BYTES` bytes and
  `_bounded_settings` resolves the family to the mark's order before decoding.
  Measuring the first draft mattered: with `max_bytes=4` the utf-32 case
  retained the whole 4-byte mark and passed vacuously, and it was only the
  measurement under mutation that showed it — the parametrization now uses 6,
  which keeps a whole code unit while cutting the mark. With
  `_bounded_settings` bypassed all three cases fail with `UnicodeDecodeError`;
  with it, all three pass. Date/Author: 2026-10-17, implementation agent.

- Observation (review round 4, `typos.toml`): one finding was against
  `typos.toml`. That file is regenerated by `make spelling` from the live
  shared estate dictionary plus `typos.local.toml` on every run, and AGENTS.md
  forbids hand-editing generated entries; the narrow-exception route is
  `typos.local.toml`, and the finding named no repository-specific word that
  would justify an entry there. Recorded as a reasoned skip rather than an
  edit. Date/Author: 2026-10-17, implementation agent.

- Observation (review round 5, the BOM fix's own blind spot): resolving a
  mark-selected codec on *every* finalization fixed the cut stream and broke
  the whole one. `utf-16`, `utf-32`, and `utf-8-sig` strip a leading mark only
  under the family name; under the concrete codec the mark survives as a
  leading U+FEFF. A bound of 4096 over `"abc".encode("utf-16")` therefore
  reported `'﻿abc'` where `FullCapture` reported `'abc'`, on all three families.
  `_bounded_settings` is now consulted only when `self._dropped` is truthy. The
  lesson is that the round-4 test asserted a suffix relation only and used only
  bounds that cut the mark, so it could not see the uncut case; the new case
  compares against a real `FullCapture` over the same bytes rather than against
  an independently computed expectation. Date/Author: 2026-10-18,
  implementation agent.

- Observation (review round 8, restoring `IOOptions` cost a second ceiling
  breach): `cuprum.sh.output.IOOptions` was importable on `main`, and the
  2026-10-14 split that moved the class to `cuprum/sh/_deprecated.py` did not
  leave the old path behind. Restoring it needs a module-level `__getattr__`,
  because a real import is a cycle, and the adapter plus its docstring took
  `output.py` from exactly 400 lines to 439 — `C0302` again. The four
  resolved-value properties were extracted to `cuprum/sh/_resolved_options.py`
  as a `__slots__ = ()` mixin, which returns the module to 359 lines. Verified
  at run time under `python -I`: the properties resolve through the base class,
  `IOOptions is cuprum.sh._deprecated.IOOptions`, the deprecation warning still
  fires on construction, an inherited `resolved_capture` works on the subclass,
  and the instance still has no `__dict__`. The ADR-007 addendum records why
  this reverses the earlier refusal to move `resolved_capture`: all four move
  together, and the alternatives were a suppression or a broken public path.
  Date/Author: 2026-10-18, implementation agent.

- Observation (review round 8, a rule that read the shorthand): the
  redirection guard in `cuprum/sh/stdio_rules.py` asked
  `options.capture or echo_*`, so
  `RunOutputOptions(capture=False, capture_stdout=FullCapture(),
  stdout=StdioTarget.path(…))`
  was accepted — a redirect to a file with a policy told to capture the stream
  that has no parent-side pipe left to read. The guard now reads
  `options.resolved_capture[0 if name == "stdout" else 1] is not None`, which
  is the same resolved view the drain uses. The two new parametrized cases fail
  with the shorthand restored and pass with the fix; the message text is
  unchanged, so the four existing rows that assert its `capture=False` and
  `echo=False` substrings still hold. `_stdio_binding_guards.py` is untouched.
  Date/Author: 2026-10-18, implementation agent.

- Observation (review round 8, a property that asserted its own arithmetic):
  the bounded-capture suffix property reconstructed the retained tail as
  `raw[-max_bytes:]` and then asserted the accumulator's payload agreed with
  that slice — which is true of *any* accumulator whose record happens to name
  the same slice. Re-encoding the accumulator's own finalized payload recovers
  the bytes it really kept, so the property is now about the accumulator rather
  than about the record's arithmetic. The re-encoding is not the literal
  "return the finalized payload": `dropped_bytes` can exceed the overflow when
  the trim cuts a code point, so the record and the payload are deliberately
  two different measurements and `_Retained` keeps both. Date/Author:
  2026-10-18, implementation agent.

- Observation (the encoding classifier's second probe was dead weight): the
  first `_is_single_byte` compared the length of decoding all 256 byte values
  against 256, on the theory that a multi-byte codec would decode some bytes
  away. Mutation 5 removed that comparison and *no* test failed, which is the
  signal to check whether the probe can ever fire rather than to add a test for
  it. Measured over the whole registry, the pending-byte probe never admits a
  codec the length comparison rejects — a codec either holds an incomplete byte
  (caught by `getstate()`) or emits one character per byte, and there is no
  third case for a text codec. The comparison was removed, `_ALL_BYTES` with
  it, and the docstring rewritten around the pending-byte definition the
  remaining probe actually implements. The 48 cases still pass. Date/Author:
  2026-10-18, implementation agent.

- Observation (review round 8 follow-up, a wrapper the tree's own rule
  rejects): the spill-I/O seam named each blocking call as a one-instruction
  function — `_write_to_spill(file, data)` and four siblings — so the
  accumulator could await them through `_SpillIo.run`. The DF12 pass rejected
  every one as `R9104` (trivial-attribute-wrapper): a body that is a single
  statement forwarding an attribute access rooted at its own parameter is a
  wrapper by definition, however much a docstring explains it. The rule's
  documented remedy is to supply the attribute or bound method at the call
  site, so `_SpillIo.run(file.write, chunk)` replaced the thunk and the five
  functions were deleted. The seam keeps the one call that must exist as a name
  — `_create_spill`, which the failure tests patch — and a traceback now names
  the method that actually raised rather than a forwarder. The lesson: a helper
  that exists only to be awaited is invisible to review and visible to the
  linter, and a suppression would have hidden a real indirection. Date/Author:
  2026-10-18, implementation agent.

- Observation (review round 8 follow-up, a timeout that was the host and not
  the code): after the round-8 fixes, `make lint` failed at
  `pylint-integration`, with two of the seven
  `cuprum/unittests/test_pylint_runtime_contract.py` tests each reporting
  `Failed: Timeout (>30.0s) from pytest-timeout` while parked in
  `subprocess.run(...).communicate`. Every measurement said host load rather
  than defect: the load average was 7.56/10.71/14.64; a warm DF12 `uv tool run`
  bootstrap took 189-410 ms and a direct DF12 pylint on a one-line fixture 728
  ms, so the steady-state work is around a second against a 30 s bound; each
  failing test passed alone under `--timeout=180` (0.34 s and 0.90 s); the
  module then passed 7/7 at `--timeout=180` in 3.85 s and 3/3 consecutive times
  at the unmodified 30 s bound (4.04/3.96/4.04 s); and the next full gate run
  passed the same module in 4.22 s with `make lint` exiting 0. No code changed
  for this. The lesson: a `pytest-timeout` report inside a `uv tool run` leaf
  is a statement about the machine's scheduler as much as about the code, and
  the way to tell the two apart is repeated runs at the enforced bound, not
  loosening the bound. Date/Author: 2026-10-18, implementation agent.

- Observation (review round 10 follow-up, the duplication entry moves with the
  import): `94184c222` routed the line-stream cleanup's two inline
  `_DrainContext` constructions through `_drain_context`, which added
  `_await_exit_or_writer_failure`, `_drain_context`, `_reconcile_run_tasks`, and
  `_wait_for_exit_code_within_timeout` to
  `cuprum/_line_stream/coordinator.py`'s `_subprocess_wait` import. The
  reasoned exception then named only `_subprocess_stream_run.py` and
  `_subprocess_stream_exit.py`, so the family it covered had grown a third
  member: the gate reported the entry stale *and* the surviving family
  blocking, failing `make lint` at the Python half. The gate's own report drew
  the distinction that mattered — it named the three-member overlap and said
  the entry was superseded rather than merely coincidental — so the fix was to
  add the coordinator and say why, not to delete the entry. A deduplication
  that moves an import into a new module moves that module into every import
  family the import belongs to, which the gate cannot infer from the source
  edit alone. Date/Author: 2026-10-18, implementation agent.

- Observation (review round 10 follow-up, a gate leaf the host refused): after
  the duplication fix, `make lint`'s Python half passed in full — ruff,
  interrogate, both Pylint interpreters, the DF12 pass, ambrleaks, Skylos, and
  the duplication gate — and then `lint-clippy` failed with
  `build-limits: the supervisor refused: unhealthy` before any compiler ran.
  `build-limits-status` reported `capacity 4, held 2, UNHEALTHY`, a `live_work`
  drain fault, `visible compiler roots=0`, and two `server-drain` groups held
  for over 1050 s (netsuke's and pyo3's). This is the host's admission
  supervisor, not this branch: nothing about a `pyproject.toml` exception can
  make a `rustc -vV` probe be refused, and the required recovery is
  `build-limits-recover`, which is the operator's job. The non-Rust gates were
  run instead and all passed; the Rust leaves stay unobserved until admission
  recovers. Date/Author: 2026-10-18, implementation agent.

- Observation (review round 11, the mutation probe has to reach the window it
  names): the first form of the new drain regression released the spill's own
  write (write #1) and then waited for the next write, expecting the append to
  be gated. The gate held nothing, and the case failed after ten seconds with
  "the spill never held its write 1 open" — the double was created with
  `block_on=(2,)`, so write #1 returned unblocked and write #2's event was the
  only one ever waited on. The lesson is in the failure text: because the
  double exposes *which* write it is holding rather than a bare "started" flag,
  the case could say exactly which write it never saw. A second pitfall sits
  beside it: the shielded write completes even when the awaiting coroutine is
  cancelled, so the assertion that proves the fix is that the *returned* record
  counts the chunk, not that the file contains it. Date/Author: 2026-10-18,
  implementation agent.

- Observation (review round 10 follow-up, a refused probe is cached): the
  `test_pinned_doctest_route_rejects_a_warning` failure that survived three
  `make test` runs was not the supervisor's current health. Cargo caches each
  `rustc -vV` probe in `$CARGO_TARGET_DIR/.rustc_info.json`, and that file in
  `rust/target/doctest-warning-contract` had recorded `"success": false`,
  `"status": "exit status: 1"`, with the refusal text as the output, from a
  window when admission really was refusing. Cargo replays a *cached failure*
  without re-probing, so the test kept failing with
  `the supervisor refused: unhealthy` while `build-limits-status` reported
  healthy and the same command against a fresh or cleared target directory
  exited 0. The tell is the file itself: a poisoned cache names the failure it
  will replay. Removing `.rustc_info.json` fixed it; `CARGO_CACHE_RUSTC_INFO=0`
  also does, without touching the directory. The harness is identical on the
  base `d93df9563`, so the fixture is inherited and no branch change is owed.
  This also means a `build-limits:` refusal can outlive the condition that
  produced it, so a repeatedly-refused compiler probe should be checked against
  `build-limits-status` before it is attributed to the branch. Date/Author:
  2026-10-18, implementation agent.

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

- Decision: the policies also apply to `run_bytes()`, and the byte-exact
  results report them. Rationale: issue #443 is about the `str` result, and the
  first cut scoped the policies to the text path. That left `run_bytes()`
  silently ignoring a policy the caller had named, which the issue does not
  sanction. Applying them costs one branch — a spilled stream is read back —
  and a byte run's payload cannot say on its own whether a policy shortened it.
  `BytesCommandResult` therefore carries the same keyword-only `captures` tuple
  as `CommandResult`, filled from the same drain records at both construction
  sites, and `BytesPipelineResult.captures` reads the final stage's. The
  `bytes | None` payload fields keep their annotation; the read-back remains
  O(stream size), which is documented in `docs/users-guide.md`. Date/Author:
  2026-10-14, implementation agent; rewritten after review, and revised when
  review found the byte result classes could not report a spill.

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

- Decision: keep the resolved capture pair in a non-init `_resolved_capture`
  field rather than writing it back over `capture_stdout`/`capture_stderr`.
  Rationale: `dataclasses.replace` re-passes every init field, so a resolution
  stored in one returns to the constructor as an explicit per-stream
  declaration and outranks the replacement's shorthand. The consequence was
  silent: `replace(options, capture=False)` kept capturing both streams, so
  every caller adapting shared options would lose the ability to turn capture
  off. A non-init field is never copied, so a replacement resolves from the
  fields the caller actually wrote; `_synthesized_sink` already sits there for
  the same reason. Date/Author: 2026-10-17, implementation agent; raised by
  review.

- Decision: defer the spill file's flush to `_finalize_spilled`, and track
  `_flushed_bytes` separately from `_spilled_bytes`. Rationale: `_append` runs
  once per chunk on the run's event loop, so a flush there is one blocking
  syscall per read on the loop that also drives the other stream, the deadline,
  and cancellation; nothing reads the file before finalization and the close
  flushes whatever is left. Once the flush moves, the two counts can diverge,
  and a failed flush must report the bytes that reached the file rather than
  every byte handed to the buffered writer — which is what
  `test_a_failed_append_flush_is_reported_as_a_flush_failure` asserts.
  Date/Author: 2026-10-17, implementation agent; raised by review.

- Decision: compose each drain-then-release pair into one coroutine so a single
  `_shielded_cleanup` covers both, rather than shielding the two steps
  separately. Rationale: `_shielded_cleanup` finishes *its* coroutine and then
  re-raises the caller's cancellation; two sequential shielded calls are not
  one shielded sequence, so a cancellation between them leaves the second step
  unrun. That is exactly the state these paths exist to avoid — sinks unclosed
  and spill files on disk — and the `lines()` completion path, the pipeline
  timeout and failed-assembly paths, the spawn-failure path, and the
  single-command finalize were all affected. In the spawn-failure case the
  gather is kept first inside the composed coroutine, because a stage task the
  cancel has not finished unwinding may still be appending to the file.
  Date/Author: 2026-10-17, implementation agent; raised by review.

- Decision: each capture release sits in a `finally` around its observe-task
  drain, at every pipeline failure path, rather than relying on the composed
  coroutine's statement order. Rationale: composing the two halves made them
  survive a *cancellation* together, which was the round-2 concern, but the
  drain's own failure is a separate exit. `_drain_tasks_during_cleanup` raises
  a group when an observe task fails, so the release it preceded was skipped
  exactly where the run was already failing. A `finally` covers both exits and
  does not reorder the two: the release still runs after the cancels, which is
  what keeps a cancelled consumer from racing the close. The spawn-failure
  gather gets the same treatment so its ordering is an invariant rather than a
  consequence of `return_exceptions=True`. Date/Author: 2026-10-17,
  implementation agent; raised by review.

- Decision: continue past the scope tolerance rather than stopping at the
  30-tracked-file / 2500-net-line trigger. Rationale: measured against the
  rebased base `origin/main` at `d93df9563`, the branch at `fcbd5b258` changes
  118 tracked files and nets 12564 lines (13736 added, 1172 deleted), so both
  thresholds are breached several times over. That figure was re-derived for
  the rebase and then re-measured once the post-rebase repairs landed; an
  earlier reading of 112 files and 11121 net lines (12265 added, 1144 deleted)
  was taken at `220ee9ca0` and describes that tree. Against the pre-rebase
  merge base `304779c25` the same branch measured 109 files and 10826 net lines
  (11950 added, 1124 deleted); that measurement now describes the history it
  was taken from rather than the branch as it stands, and it is recorded here
  dated rather than carried forward. The tolerance exists to catch a task that
  has quietly become a different task; here the extra scope is the feature
  itself plus the tests and documentation that witness it. Every acceptance
  criterion in issue #443 needs its own module, unit suite, behavioural
  scenario, or design prose, and the source plan's four tasks already enumerate
  work of this shape. Stopping would leave a half-delivered API — policies
  declared but not threaded through the single-command, pipeline, `lines()`,
  and byte-exact paths — which is a worse outcome than a large branch that
  passes every gate. The plan defines no approver to escalate to, so the
  decision is recorded here rather than referred elsewhere; the individual
  splits that kept each file under the 400-line cap are entries of their own in
  this log and in ADR-007's addenda. Date/Author: 2026-10-18, implementation
  agent; raised by review.

- Decision: delete the duplication allow entry for
  `cuprum/_process_handle/coordinator.py ~ cuprum/_subprocess_stream_post_exit.py`
  rather than widening it or reasoning it back into place. Rationale: the
  entry was written when the post-exit module still imported the
  `_subprocess_wait` run machinery and `_shielded_cleanup`. The rebase rebuilt
  that module around `_settle_owned_group_before_draining` and
  `_await_stdin_writer`, and it now imports only `_terminate_all_shielded`,
  `_settle_stdin_writer`, and `_TeardownPolicy`, while `coordinator.py` still
  imports `_shielded_cleanup`, `_terminate_process`, and the `_subprocess_wait`
  trio. No key of the entry matched any family the scan reported, so the
  overlap it reasoned about no longer existed in any form. The deletion was
  made only after confirming that against the source, including the file as it
  stood before the commit that introduced the entry, because an unmatched entry
  is not by itself proof that the duplication is gone. Date/Author: 2026-10-18,
  implementation agent; raised by the duplication gate's stale-entry advisory.

- Decision: re-derive the scope figures for the rebase rather than increment
  the pre-rebase ones. Rationale: the scope-tolerance entry above measured 109
  files and 10826 net lines from the merge base `304779c25`, which the rebase
  removed from this branch's history. Against the new base `d93df9563` the
  branch at `fcbd5b258` is 118 files and 12564 net lines (13736 added, 1172
  deleted). Both measurements are retained, each labelled with the base it was
  taken against, and the decision itself is unchanged: both tolerance
  thresholds are still breached several times over. Date/Author: 2026-10-18,
  implementation agent; raised while preparing the final gate evidence.

- Decision: restore `cuprum.sh.output.IOOptions` through a module-level
  `__getattr__`, and pay for it by moving the four resolved-value properties
  into a `__slots__ = ()` mixin in `cuprum/sh/_resolved_options.py`. Rationale:
  `IOOptions` was importable from `cuprum.sh.output` on `main` and the
  2026-10-14 split moved the class without leaving the path behind, so a caller
  importing it from the old location gets an `ImportError` — a break this
  branch introduced, not a deprecation it documented. The lazy resolver is the
  only cycle-free way to publish a name whose module imports this one. It costs
  39 lines against a 30-line headroom, so the ceiling failed again; the mixin
  is the only option that is neither a suppression nor a broken public path.
  The cut is a real one: the properties are the *query* half of the options,
  deriving from fields that stay on the dataclass. Alternatives rejected: a
  `too-many-lines` suppression (the standing contract); re-exporting from
  `cuprum/sh/_deprecated.py` instead (that is the module that imports this one,
  and the finding is that the *old* path must work); and moving
  `__post_init__`'s validation out instead, which is the command half and would
  split one constructor's contract across two modules. This reverses the
  2026-10-14 refusal to move `resolved_capture` alone, on the ground it had to
  mirror `resolved_echo`; moving all four keeps that invariant. Recorded as a
  fifth split in the ADR-007 addendum. Date/Author: 2026-10-18, implementation
  agent; raised by review round 8.

- Decision: the stdio redirection guard reads `resolved_capture` rather than
  the `capture` shorthand. Rationale: the rule exists because capture needs a
  parent-side pipe, and since this branch the request for capture is per
  stream. Reading the shorthand made a redirect plus a `capture_stdout` policy
  legal at construction and left the run with no pipe to drain the stream it
  had been told to retain. `__post_init__` fills `_resolved_capture` and both
  resolved echo fields before `_validate_stdio_targets`, so the rule can read
  the resolved views with no re-resolution and no new import. The message text
  is deliberately unchanged: four existing parametrized rows assert its
  `capture=False` and `echo=False` substrings, and widening the check does not
  change what the caller has to do. Date/Author: 2026-10-18, implementation
  agent; raised by review round 8.

- Decision: refuse a bounded text capture under an unrepairable codec before
  spawning, rather than tracking decoder state while the bound absorbs bytes.
  Rationale: the report offered both routes, and the stateful one cannot
  deliver what it appears to promise. Resolving a boundary *after* the head has
  been dropped means knowing which byte in the retained tail is a lead byte,
  which a stateful codec only records from the start of the stream — so the
  accumulator would have to keep the whole stream, exactly the memory the bound
  exists to avoid. Refusal is also the only option that cannot be silently
  wrong: the alternatives are to invent a character or to ignore the bound, and
  a caller who wanted either would have said so. Restricting the *encodings*
  rather than the *bound* keeps the policy's other promise intact — the bound
  is still exactly a byte count, and a caller who needs a multi-byte codec as
  text has `FullCapture` and `FileBackedCapture`, which do not truncate the
  leading edge. Classifying by probing rather than by a codec list is what
  keeps the rule true as the registry changes; the one cost is that the probe
  runs at each pre-spawn seam, which is bounded by 256 decoder constructions
  and is small beside the spawn it precedes. Date/Author: 2026-10-18,
  implementation agent; raised by review round 8.

- Decision: publish the spill handle before the prefix is written, and clear
  its keep policy until the write succeeds. Rationale: the handle is what
  teardown reconciles, and the natural ordering — publish once the spill
  "worked" — leaves a `delete=False` file that only the accumulator knows about
  across two awaits. A cancelling drain never settles, so no `CaptureFinal` is
  built for the result-construction site and nothing carries the path; the
  registry can only reach the *sink*, which is why the sink must hold the
  handle from the instant the file exists. `keep=False` on the provisional
  handle is the deliberate part: `keep_file=True` promises the caller a file
  the run finished writing, and a file a cancelled spill never wrote has not
  earned that exemption. Promotion sets `keep` from the policy once the flush
  returns, so the successful path reports the same ownership it always did.
  Date/Author: 2026-10-18, implementation agent; raised by review round 9.

- Decision: import `NamedTemporaryFile` by name in `_capture_spill_io`
  rather than reaching through `tempfile`. Rationale: patching
  `cuprum._capture_spill_io.tempfile.NamedTemporaryFile` mutates the standard
  library's module object, so the substitution reaches every importer for as
  long as the patch lives and never exercises this module's own binding at all.
  Binding the name at import makes the module the seam, which is the seam the
  failure tests assert on. The cost is that the leaf names its one dependency
  rather than the namespace, which reads better in the create function anyway.
  Date/Author: 2026-10-18, implementation agent; raised by review round 9.

- Decision: create the spill file and adopt its handle inside one shielded
  step, rather than publishing between two awaits. Rationale: round 9 moved the
  publish ahead of the write, which closed one window and left the earlier one
  open. The create itself is an `await` on a worker, and a cancellation that
  arrives while it runs is re-raised only once the worker finishes — by which
  time the `delete=False` file exists. A publish bound after that await is
  therefore never reached on the cancelled path, and the file is orphaned
  exactly as before. `_run_shielded_step` reports the value even when it
  reports a cancellation, so adopting before either is raised makes "the file
  exists" and "teardown can reach it" the same fact. The order at the end of
  the step is deliberate: a value is adopted first, then a failure is raised,
  then a cancellation. A create that failed leaves no file, so there is nothing
  to adopt and either exception is equally safe; a create that succeeded and
  was cancelled must adopt before it raises. Date/Author: 2026-10-18,
  implementation agent; raised by review round 10.

- Decision: keep the five-second run deadline in `test_capture_timeout.py` and
  synchronize on the child's readiness marker instead of shortening it.
  Rationale: the deadline is not the thing under test — the capture the policy
  retains is — and lowering it would trade one scheduling race for another. The
  marker says the child has flushed its prefix; waiting for it makes the
  assertions below it observable facts rather than bets on interpreter
  start-up. The helper lives in `tests/helpers/timeouts.py` because a second
  copy in the capture suite would be the third implementation of the same poll.
  Date/Author: 2026-10-18, implementation agent; raised by review round 10.

- Decision: correct the users'-guide and roadmap claims to scope the
  code-point guarantee to text capture, rather than changing the byte path to
  trim. Rationale: `run_bytes()` asks for the child's own bytes. Trimming a
  tail to a code-point boundary would discard bytes the caller explicitly
  requested, and the whole point of the byte-exact entry point is that no
  decoding decision is imposed on it. The documented guarantee was wrong, not
  the code. Date/Author: 2026-10-18, implementation agent; raised by review
  round 10.

- Decision: widen the import-family exception to a third member rather than
  extracting a shared import. Rationale: the detector reports a *span of import
  declarations*, and each module must bind the name into its own namespace to
  call it. A shared helper cannot remove an import; it can only add one. The
  entry therefore records the dependency surface each module declares, and its
  membership must track which modules declare it. The gate already reports a
  superseded entry by name, which is why the stale line and the blocking line
  arrived together — the entry was widened rather than deleted because the
  duplication is the intended prologue of a deliberate split. Date/Author:
  2026-10-18, implementation agent; raised by review round 10 follow-up.

- Decision: correct four prose claims about spill readability, rather than
  changing the removal behaviour to match the prose. Rationale: the file's
  lifetime is owned by the policy, and `keep_file=True` is the documented way
  to hand it over. Making every failed read-back keep its file would leave
  debris in the temporary directory precisely when the run is least able to
  account for it, and it would make `keep_file` mean two different things
  depending on a failure the caller did not ask for. The claims were wrong, not
  the cleanup: the design doc's "left where it is for the caller to read", the
  ExecPlan's post-run path promise, its unqualified `None` claim for a
  file-backed stream, and O9's conflation of the default with the
  `capture=False` opt-out. Date/Author: 2026-10-18, implementation agent;
  raised by review round 10 follow-up.

- Decision: name `capture_stdout`/`capture_stderr` in the redirection refusal
  rather than only the `capture` shorthand. Rationale: the rule reads the
  *resolved* per-stream policy, so `capture=False` is not always a remedy. With
  `capture_stdout=FullCapture()` it is already false and the field is what
  still asks for the pipe, which made the old message an instruction that
  changed nothing. The message keeps the shorthand as the alternative for a
  stream whose field is unset, so both the shorthand-only and the field-only
  caller have a correct next step. Date/Author: 2026-10-18, implementation
  agent; raised by review round 10 follow-up.

- Decision: split the bounded-capture suite by *question asked*, not by test
  kind, and share the fixtures through a support module. Rationale: the
  789-line module held — and still held 754 lines at the split point, after the
  drain drivers moved out — three suites that answer different questions: does
  the drain reach the accumulator, does retention hold for any stream, and what
  happens at each boundary. Two of the three would pass unchanged if the drain
  never consulted the config's sink. Naming that in each module's docstring is
  what keeps a future edit from merging them back into a list of cases whose
  relationship is implicit. The shared `_config`/`_sink`/`_Retained` fixtures
  went to `_bounded_capture_support.py` rather than being copied, because three
  inline copies of a config would drift into testing three different
  configurations. Date/Author: 2026-10-18, implementation agent; raised by
  review round 10.

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

## Review round two on the replayed head

`coderabbit review --agent` over `a40898789..2c8fce82d` (125 files reviewed)
reported four findings. An earlier run of the same review had been killed by
SIGTERM before it finished, discarding its results; the five leads it had
streamed were triaged against the code instead, and every one that proved
genuine was acted on here too. All the changes below are documentation,
test-documentation or dead-code corrections in this branch's own surface; none
changed runtime behaviour.

- **O5 over-promised the kept spill file.** It said the file survives with
  "complete contents" for every outcome, which is false on the timeout and
  cancellation routes: the file holds the bytes received before the run
  stopped. O5 now splits the two returning routes (whole stream) from those two
  (received prefix) and points at O8, which already stated the partial
  behaviour. Discharging evidence for the split already existed:
  `test_a_kept_spill_file_survives_a_timed_out_run_whole` asserts the flushed
  prefix, not the whole payload.
- **The users' guide's policy table claimed a file unconditionally.** Below
  `spill_threshold_bytes` the capture never leaves memory, and
  `test_a_stream_under_its_threshold_is_not_a_witnessed_spill` is the negative
  control that pins it. The `FileBackedCapture` row now says the file appears
  only past the threshold.
- **The UTF-16 realignment docstring named the wrong dropped prefix.** A
  scratch probe confirmed the review's arithmetic: the 10-byte payload
  `a中b文c` bounded at 7 starts its tail at offset 3, realignment moves it to
  4, and the survivors are `b文c` — which is what the assertion already pinned.
  Only the sentence was wrong.
- **`_DecodeSettings.max_code_point_bytes` was dead.** No trim step read it,
  its table keyed `latin-1` while the codec registry's canonical name is
  `iso8859-1`, and `_trim_to_code_point`'s `Returns` section stated a
  shortening bound of `max_code_point_bytes - 1` that the two-edge trim does
  not honour. The field, its `__post_init__` and the table are removed; the
  constant the UTF-8 scan actually uses remains, and the bound is now prose
  that describes one partial code point lost at each edge.
- **`_capture` in the behaviour suite asserted truthiness.** It returned
  `matching[0]` after any non-empty match, so a second record for the same
  stream would have been silently ignored. Two records are not reachable today —
  `_recorded_captures` flattens a two-slot pair and `PipelineResult.captures`
  forwards the final stage's tuple — but the helper now requires exactly one
  and names the whole tuple when it fails.

A sweep for the same class of defect found four more module docstrings on this
branch that narrated the rebase or branch history ("while this branch was in
flight", "when the rebase onto ``main`` pushed that module past the 400-line
ceiling"). Those were rewritten to state the constraint rather than the
incident. The sweep was scoped to lines this branch's diff adds: the same
phrasing appears in several files inherited from `main` (the module splits
recorded by `_stdio_ownership.py`, `pipeline_output.py` and friends), and
rewriting those would be unrelated churn.

Two phrasing hits were *not* changed because they use "branch" in its other
senses: `_stream_drain_finish.py`'s "keeps this branch" is a code path, and
`test_broken_pipe_echo_snapshot.py`'s "edited on this branch" is a test-double
rationale about sibling modules rather than a historical narrative.

## Review round three on the pushed head

`coderabbit review --agent` over the pushed head `418ca8dc9` (125 files
reviewed) reported three findings, all minor or trivial, and all documentation.
Each was checked against the code before being acted on; none changed runtime
behaviour.

- **The guides over-promised `keep_file=True` under cancellation.** If
  cancellation lands before the initial spill's prefix is written,
  `_publish_spill` has published the handle with `keep=False` and
  `_promote_provisional_spill` has not run, so `_FileAccumulator.cleanup`
  closes it with `remove=not handle.keep` and the file goes even under
  `keep_file=True`. The users' guide, the migration guide, the design document,
  and the `keep_file` parameter now state that exception, and O5 above names it
  too. The code is unchanged: the ordering is deliberate and is recorded in the
  ADR-007 addendum.
- **The users' guide implied a storage failure always leaves a result.** A
  removal failure on a timeout or cancellation route is absorbed, but there is
  no result on that route to attach anything to; the users' guide and the
  design document now say the run's own outcome stands and reserve "receives a
  result" for the returning routes.
- **The spill directory's latency was undocumented.** `_run_off_loop` shields
  every worker call through `_shielded_cleanup`, so a write blocked on a
  stalled mount cannot be abandoned: `_settle_consumers` gathers the task, and
  the timeout or cancellation waits with it. The `directory` parameter's
  docstring and the users' guide now say to use a local, responsive filesystem
  when the deadline guarantee matters. The suggestion to emit a time-based
  warning was not taken: a wall-clock threshold on a shielded call is a new
  observable and a new tuning knob, and the cancellation contract does not
  depend on it.

`capture_policy.py` sits at the 400-line module ceiling, so the two added
parameter notes were paid for by tightening the surrounding docstring prose
rather than by growing the module. After the review the same claim was found
untested in the other prose that describes `keep_file`: the design document and
the migration guide both promised the kept file on every exit route, so both
were corrected in the same commit.

## Review round four on the pushed head

`coderabbit review --agent` over the pushed head `2239573eb` (125 files
reviewed) reported ten findings. Every one was checked against the code before
being acted on; none changed runtime behaviour, and every one was valid.

Documentation and docstrings:

- **The `PipelineResult.captures` docstring denied interior stderr.** It said
  only the final stage's streams are captured. `_create_stage_capture_tasks`
  builds a stderr consumer for every stage whose `consumes_stderr` holds, and
  `_build_stage_result` attaches `stderr_capture` to every stage result, so an
  interior stage's stderr — truncation, spill, and storage-failure records
  included — lives on `stages[i].captures`. The design document already said
  so; the docstring and `BytesPipelineResult.captures` now say it too, and the
  users' guide's capture-metadata section gained the same paragraph.
- **Three prose corrections.** "as it always has been" became "as it has
  always been"; the `CaptureSink` ownership paragraph lost a comma before
  "when", "the await costs a coroutine step" became "awaiting costs one
  coroutine step", and "by accident" became "inadvertently"; the changelog's
  capture-resolution sentence was split so the example and the compatibility
  claim each get their own sentence.

Test-strength corrections, where the finding was that a case could pass for the
wrong reason:

- **The discard-drain release case held a sink in the stdout slot only.** A
  release that touched only the first slot passed. A second `_ReleasedSink` in
  the stderr slot is now asserted too, and a probe that narrowed the release to
  `captures[:1]` fails the new assertion.
- **The refusal cases could not tell "absorbed a refused unlink" from "never
  tried".** `_RefusingClose.unlink` counts its attempts, and both cases assert
  exactly one. A surviving file was doing double duty as the witness for
  removal having been attempted, which it cannot be.
- **The shared-directory case could not see a "remove everything" cleanup.**
  With no file the run did not own in the directory, an empty listing was
  equally consistent with a cleanup that deleted the whole listing. A bystander
  file is written before the runs and asserted to survive both.
- **Two vanished-file cases read the private `accumulator._path`.** They now
  discover the sole entry in the policy's directory, so the cases do not pin an
  internal field name. The text-mode message says "spill file", which is what
  that policy produced.
- **Two `monkeypatch.setattr` installers wrapped their value in
  `typ.cast("typ.Any", ...)`.** `setattr` takes a plain object, so the casts
  bought nothing and added an `Any` the guidelines require be justified.
- **The policy tests split on `isinstance()`.** Both splits are now `match`
  statements, as the project's style asks.

The first certification run at the round-four commit failed `make lint`: the
new interior-stderr paragraphs pushed `cuprum/sh/results.py` from exactly 400
lines to 401, and `pylint-classic` reported
`C0302: Too many lines in module (401/400)` before any later lint leaf could
run. Trimming the two `captures` docstrings brought the module to 398. Because
the commit had never been pushed, it was amended rather than fixed forward,
which orphaned the lint log and every gate result recorded against the
pre-amend head. Lesson: a docstring edit is a size change too, so check the
module length before starting a long gate run, not after it.

The cert5 run then failed `make lint` a second time, on the two `match`
statements the same round introduced: `pylint` 4.0.9's DF12
`match-class-bind-self` (R1905) reads `case bool(enabled):` and
`case str(name):` as attribute lookups on `self` and asks for
`case bool() as enabled:` and `case str() as name:` instead. The forms match
the same values, and `5bf5d9325` restores the capture spelling. Lesson: the
DF12 pass uses a message set that no other leaf shares, so a local lint of the
changed files should run with `--enable` set to the repository's exact
`DF12_PYLINT_MESSAGES` list before a certification is requested, not just plain
`pylint` or ruff.

## Review round five on the pushed head

`coderabbit review --agent` over the pushed head `683513cb1` reported four
findings: one major and three trivial. Three were acted on as written and one
in part.

- **The `utf-16`/`utf-32` rationale was wrong, and the finding was right about
  that.** The docstrings said the family decoders "accept no bytes at all
  without" a mark and that "the platform's decoder rejects a headless stream".
  A probe on this little-endian host returns `"a"` for
  `b"a\x00".decode("utf-16")`, so a *one-shot* family decode certainly accepts
  a headless stream. Probing further found the narrower truth: the
  *incremental* decoder that `_pending_bytes` consults raises
  `UnicodeDecodeError: ... Stream does not start with BOM` for a first chunk
  with no mark, under both `strict` and `replace`. The corrected text names
  which decoder each claim is about instead of choosing a side, and keeps the
  finding's real reason: a dropped mark that selected the non-native order
  would be decoded in native order and produce wrong text.
- **The ADR-007 addendum had no empty line artefact to remove.** The finding
  asked for one as well as for the ownership rewrite. `git diff --check` exits
  0 and lines 1258–1300 contain no consecutive blank lines, so only the rewrite
  was actionable.
- **The 400-line limit does not bind test modules.** The finding treated the
  533-line `test_capture_spill.py` as a violation. It is not one: test targets
  run pylint with `--disable=too-many-lines`, and `origin/main` carries test
  modules up to 959 lines. The split was made anyway — it separates the two
  cancellation drivers from the fidelity cases — with the moved tests still
  collected.

The certification run at `dc6e5c0a9` was red, on two counts that are worth
recording because neither is a defect in the feature:

- `make check-fmt` failed at its mdtablefix leaf on the execplan addition
  itself. A literal `→` in the round-five record made mdtablefix's proximity
  rule pull the following word onto the arrow's line, so a document that had
  passed `make markdownlint` minutes earlier still failed the formatter.
  `9bf2e4c27` rewrites the arrow as `returns`, which is stable under the
  formatter. Lesson: `make markdownlint` does not run mdtablefix, so prose
  containing symbolic punctuation needs `make check-fmt` (or a bare mdtablefix
  `--check` with the same flags) before a certification is requested.
- That repair was committed while `make lint` was running, so the lint log
  opens at `dc6e5c0a9` and closes at `9bf2e4c27`: the Python and Rust leaves
  ran before the commit and the spelling, yamllint and actionlint leaves after
  it. An exit code of 0 across two heads certifies neither. No gate may be
  started unless the tree can be left alone until all gates finish, and the
  remaining four gates were correctly abandoned rather than run against a
  drifting head.

Date/Author: 2026-10-19, implementation agent.

## Second replay onto upstream main

The first replay was cut against `d93df9563`. Upstream then advanced to
`f359f2ec4`, and PR #635's merge ref would have been built on a base that
already carried newer work in the same modules, so the series was replayed
again from the preserved child commits rather than merged forward. The replay
boundary is unchanged: `a40898789..83c1fefeb`, 106 child-owned commits, with
the parent PR's squash commit `d93df9563` excluded as inherited history.

Recovery material, retained until publication is accepted:

- `refs/recovery/issue443-head-83c1fefeb` — the pre-rebase child head.
- `refs/recovery/issue443-target-f359f2ec4` — the second target.
- `refs/recovery/issue443-old-base-a40898789` — the exclusive replay boundary.
- `refs/recovery/issue443-rebased-ef2f084bc` — the abandoned weave run.
- `refs/recovery/issue443-rebased-nonweave-ce446cf5d` — the accepted replay.
- `backup/issue443-pre-rebase-20261018` — a backup branch at the old head.

**The weave incident.** The first replay attempt ran with the repository's
configured weave merge driver. It exited 0, but the replay was corrupt: several
`cuprum/_result_assembly.py` and `cuprum/_subprocess_execution.py` hunks were
fabricated — text that neither side contained — and the corruption was only
visible by reading the replayed patches, not by the exit status. The run was
abandoned (`refs/recovery/issue443-rebased-ef2f084bc`), and the replay was
repeated with the driver disabled via `git -c core.attributesFile=/dev/null`.
Lesson: a clean exit from a merge driver is not evidence that a replay is
semantically sound; the replayed patches have to be read.

**Conflicts and resolutions.** Sixteen files conflicted, all in the execution
core (`_result_assembly.py`, `_subprocess_execution.py`, `_subprocess_wait.py`,
`_subprocess_stream_run.py`, `_pipeline_internals.py`, `_stream_drain.py`,
`sh/results.py`, `sh/output.py`, and their tests). Every resolution took the
target's newer structure and re-expressed the child change inside it; none took
a whole side. The one that matters for review is the result-class seam, below.

**The result-class reconciliation (`3581a06b3`, ADR-025).** Upstream's
`f359f2ec4` carries ADR-024, which had been written on this branch and
upstreamed: "every single-command text run that captures output constructs
`CapturedResult`". The replay initially resolved `_build_command_result` to
`if execution.capture:` — a field this branch deleted when capture became a
per-stream policy pair — and dropped `captures=` from every construction. That
predicate is both unbuildable and semantically wrong. The check that settles it
is that `CapturedResult` redeclares `stdout`/`stderr` as `str`, while a
text-mode `FileBackedCapture` that spilled reports its payload as `None`
(pinned by `test_capture_spill.py`, relied on by
`test_capture_spill_cleanup.py`), and a single-stream capture leaves the other
stream `None`. So "did the run capture anything" cannot decide the class.

ADR-025 records the narrower rule: `_build_command_result` constructs
`CapturedResult` exactly when both settled payloads are `str`, through
`_is_captured_text_pair`, and threads `captures` into all three constructions.
A default `RunOutputOptions()` run resolves both streams to `FullCapture`, so
both payloads are `str` and ADR-024's contract holds unchanged. The predicate
was probed in both directions: forced `True` fails the single-stream and spill
cases with
`_ExecutionInvariantError: text capture was enabled but stderr was not captured`;
forced `False` fails the full-capture and bounded cases. The probe was
restored with `cp` and `git diff --quiet` confirmed the file matched its
committed blob. `75ee5826e` adds the five cases that witness the rule.

**Audit of the second replay.**
`git range-diff a40898789..83c1fefeb f359f2ec4..HEAD` reports 106 entries: 100
identical (`=`), six differing (`!`). Each difference was read rather than
counted:

| Entry | Commit                                                             | Difference                                                                                                  | Verdict                       |
| ----- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | ----------------------------- |
| 1     | `Split deprecated and convenience machinery out of output options` | Hunk context only: the `sh/__init__.py` module map gains `CapturedResult`, which target carries             | Intended                      |
| 2     | `Add per-stream capture policies and capture metadata`             | `CapturedStream`'s import position in `__all__` follows the target's ordering                               | Intended                      |
| 4     | `Remove owned spill files on the single-command success path`      | Hunk header follows the target's `_require_captured_text`; the added `_recorded_captures` is byte-identical | Intended                      |
| 7     | `Document the per-stream capture policies`                         | One context line differs because target reworded the pipeline contract note                                 | Intended                      |
| 26    | `Fix review findings in capture-policy tests and docs`             | Same context line as entry 7                                                                                | Intended                      |
| 36    | `Report capture records on byte-exact results`                     | The `_build_command_result` hunk now ends at a narrower predicate, not the deleted `execution.capture`      | Intended — the reconciliation |

The file-set audit compared the child's patch against the same target at both
heads. Fifteen paths that the old child patch touched are absent from the new
one; all fifteen exist at both heads and none is child-owned, so their absence
means "identical to target", not "dropped". Five further paths the old patch
recorded as deletions (`test_captured_result_layout.py`,
`test_command_result_narrowing.py`, `test_pipeline_capture_contract.py`,
`typing_fixtures/captured_result_valid.py`, and
`adr-024-captured-command-output-types.md`) are files the *old base* lacked and
the target already carried: they are inherited parent work, correctly not
re-delivered by the child. The child's net patch onto the target deletes no file
(`git diff --diff-filter=D --name-only f359f2ec4 HEAD` is empty), and
`docs/adr-024-captured-command-output-types.md` is byte-identical to the
target, so ADR-024 is neither rewritten nor resurrected.

A second file-set check asked whether upstream added tests asserting the *old*
narrowing rule, which ADR-025 would break. `test_captured_result_layout.py`,
`test_command_result_narrowing.py`, `test_pipeline_capture_contract.py` and
`test_capture_mode_telemetry.py` all go through `require_captured()` or the
`capture=True` convenience, which still resolves both streams to `FullCapture`;
none asserts narrowing for a partial or spilled run. A targeted 172-test run
around the execution core passed at `3581a06b3` with the head recorded at both
ends of the log (`/tmp/pytest-targeted-rebase-issue443-onto-f359f2ec4.out`).

`git diff --check f359f2ec4 HEAD` exits 0.

Date/Author: 2026-10-19, implementation agent.

## Third integration: merge onto bounded output delivery

Between the second replay and publication, upstream merged PR #622 ("Support
bounded output delivery off the execution loop", issue #436), landing as
`102951f48` and advancing `origin/main` past this branch's replay target
`f359f2ec4`. The PR merge ref is built on the current base, so the branch was
merged forward onto `102951f48` rather than replayed a third time: the replay
series is still child-owned and untouched, and a merge keeps every commit's
identity visible for review.

Merge commit `fd684768f` has parents `d087247b2` (the branch) and `102951f48`
(the target). Sixteen paths conflicted; all are in the execution core, the
snapshot, the duplication allow list, or the ADRs. `git ls-files -u` is empty
after resolution and `compileall` plus an AST sweep report no syntax errors.

Recovery material, retained until publication is accepted:

- `refs/recovery/issue443-head-d087247b2` — the branch head before the merge.
- `refs/recovery/issue443-integration-base-f359f2ec4` — the previous base.
- `refs/recovery/issue443-target-102951f48` — the merge target.
- Every replay ref listed above remains in place.

**ADR numbering.** Both sides introduced an ADR 025. Main's is
`adr-025-bounded-output-delivery.md` and it landed first, so it keeps the
number; the branch-authored ADR was `git mv`d to
`adr-026-narrowed-results-only-for-complete-text-capture.md` and every
reference in `docs/` was repointed. Rewriting main's accepted ADR text was not
an option: accepted ADRs in this repository are append-only.

**Duplication allow list.** The first `[[tool.duplication_gate.allow]]` entry
grew on both sides. The merged members are the true set union (41 modules),
recomputed with `tomllib` across the two stages rather than taken from either
side's list, and every member path was checked to exist on disk. Two further
conflicts kept both sides' entries, because each names a different family. The
union turned out to be one short: the detector's TYPE_CHECKING family has 42
locations, and `cuprum/_pipeline_internals.py` was in neither parent's list.
The entry now names 42.

**First gate run at `6efc173b7`.** Three of the seven gates were red:
`check-fmt` (mdtablefix rewrapped three execplan paragraphs), `duplication`
(the three families above plus four stale entries), and `test` (one test
patched a seam the merge moved). The test failure is the interesting one: the
pipeline-config delivery test replaced `_build_idle_monitor` on
`cuprum._pipeline_config`, but `_pipeline_config` now imports
`_build_pipeline_idle_monitor` from `_pipeline_config_setup`, so the patch
bound a name nothing called and the test failed with `AttributeError` rather
than exercising the cleanup path. The patch now targets
`_build_pipeline_idle_monitor`, which is the function
`_prepare_pipeline_config` actually invokes, so the assertion still proves the
worker and sink are released on a failed setup.

The four stale entries were reconciled against source rather than deleted on
the gate's word. `_pipeline_results.py ~ _subprocess_execution.py` genuinely no
longer shares an import block;
`_command_internals.py ~ _subprocess_execution.py` lost it when
`_command_internals.py`'s TYPE_CHECKING prologue was rewritten (there is no
`import asyncio` there any more); `_bytes_run.py ~ sh/__init__.py` already
reported stale on `main` at `102951f48`, so it is pre-existing rather than
merge damage; and the single-module `unit = "cuprum/sh/results.py"` entry
covered the buffered/bytes record pair before `byte_results.py` was split out,
so it becomes a two-member entry naming both modules. With those changes the
gate reports `duplication gate passed; 69 allowed by reasoned exceptions` and
no stale entries.

**`_pipeline_config` split.** Both sides added to this module and the merge
pushed it past the 400-line ceiling. The delivery-worker, idle-monitor, and
failed-setup cleanup helpers moved to `cuprum/_pipeline_config_setup.py`; the
result is 340 lines, and the four moved names were confirmed by grep to have no
external references, so no import site changed.

**The line-path encoding guard.** Main made `SafeCmd.lines()` construct its
execution lazily, inside the async generator, so the bounded-encoding guard in
the execution bundle fires only once the caller starts iterating. The
branch-authored contract (`test_bounded_encoding_guard.py`) pins the refusal to
the call, before spawning and before consumption. The guard is therefore lifted
into `_reject_unsupported_bounded_captures`, which takes the resolved
`(stdout, stderr)` pair; the execution bundle and `lines()` both call it, and
`lines()` calls it with `capture_bytes=False` because line iteration always
returns text. Weakening the test was not an option: it states a real pre-spawn
guarantee.

**Finalization.** `_reconcile_pipeline_run_failure` keeps main's
`observers.pending_tasks` drain and composes it with the branch's capture-sink
release in `finally`, so a failing drain still cannot strand a spill file. The
two defects found while merging this path (a wrong argument and a leftover bare
local name) were fixed before the first smoke run.

Date/Author: 2026-10-19, implementation agent.

## Fourth integration: first gate run after the merge

Commit `b85f36cdd` carries the three fixes above. The next step is a fresh,
sequential run of all seven gates against that frozen head; the first run
stopped at `make test`, so the `test-rust` stage that `make test` also drives
has not yet been observed at this tree.

**Second gate run, at `7cbb726fd`.** That run is now complete. The seven gates
ran sequentially against frozen head `7cbb726fdb0b30a4822be2eafa8f13bfde3df9be`
and every one exited 0. Each log is at
`/tmp/<gate>-cuprum-rebase-issue443-onto-f359f2ec4.out` and records the head it
certifies on its first line and its own exit status and elapsed time on its
last:

| Gate                | Elapsed | Closing evidence                                             |
| ------------------- | ------- | ------------------------------------------------------------ |
| `make check-fmt`    | 1s      | `98 files left unchanged.`                                   |
| `make lint`         | 261s    | `actionlint ... -shellcheck=` clean, then `exit=0`           |
| `make typecheck`    | 2s      | `All checks passed!`                                         |
| `make markdownlint` | 34s     | `exit=0`                                                     |
| `make nixie`        | 0s      | `All diagrams validated successfully!`                       |
| `make duplication`  | 1s      | `duplication gate passed; 69 allowed by reasoned exceptions` |
| `make test`         | 560s    | `exit=0`                                                     |

`make test` drives both suites. The `test-selection` prerequisite ran first, as
the recipe orders it, and reported `77 passed`. `test-python` then runs one
pytest process per `PYTEST_TARGETS` glob or file, so the log holds 21 separate
sessions. Reading each session's own summary line:

| Selector member                                   | Modules | Result                    |
| ------------------------------------------------- | ------- | ------------------------- |
| `cuprum/unittests/test_*.py`                      | 405     | `4031 passed, 93 skipped` |
| `tests/test_ci_*.py`                              | 58      | `843 passed`              |
| the eleven named `tests/test_*.py` targets        | 11      | `92 passed`               |
| `scripts/tests/test_boundary_*.py`                | 6       | `116 passed`              |
| the three other `scripts/tests/test_*.py` targets | 3       | `12 passed`               |
| `tests/behaviour/test_[a-h]*.py`                  | 20      | `171 passed`              |
| `tests/behaviour/test_[i-r]*.py`                  | 11      | `30 passed, 7 skipped`    |
| `tests/behaviour/test_[s-z]*.py`                  | 9       | `31 passed, 13 skipped`   |
| `tests/integration/test_act_stream_parsing.py`    | 1       | `22 passed`               |

The named-target row sums the eleven one-file sessions the recipe starts (14,
19, 12, 10, 5, 4, 2, 11, 8, 4, and 3 tests); the `test_boundary_*.py` glob
collected six modules in one process and reported 116; the remaining three
script targets report 2, 5, and 5. Across all 21 sessions: 5425 passed, 0
failed, 113 skipped. 29 + 7 + 3 + 2 snapshots passed, and no snapshot was
written. The module counts come from the node IDs each session printed.

The `test-rust` stage — the part the first run never reached — ran clean:
nextest `167 tests run: 167 passed, 0 skipped` under run ID
`89dfb70e-b213-4fef-bd17-91d87f1630bb`, then three doctest binaries reporting
`ok` (`0 passed; 0 failed`, `0 passed; 0 failed`,
`0 passed; 0 failed; 3 ignored`). The `test-selection` guard ran first, as the
recipe orders it, so the selector loop above is known to have collected what
the selector claims.

The log also carries repeated
`[build-limits] <crate>: the managed sccache server is unreachable; compiling
without the cache`
lines. Per the operating instructions that is an uncached compile, not a build
failure; every crate compiled and the run finished.

Date/Author: 2026-10-19, implementation agent.

## Fifth integration: CodeScene remediation on the merged tree

The hosted CodeScene check was red at `7cbb726fd`, the last published head, and
the local engine reproduced the same findings on the merged tree. Three
production and test files carried the reported findings:

| File                                       | Finding                                                       | Repair                                                                                            |
| ------------------------------------------ | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `cuprum/_streams.py`                       | `_deliver_chunk` 48 lines, Bumpy Road                         | split into `_note_stream_activity`, `_absorb_into_capture`, `_fan_out_chunk`                      |
| `cuprum/_subprocess_execution.py`          | `_build_command_result` 75 LoC, Large Method                  | extracted `_shared_result_fields` behind a `_SharedResultFields` TypedDict                        |
| `cuprum/unittests/test_pipeline_config.py` | Over-arity, and a row pairing an accessor with the wrong sink | dropped the redundant `sink_name` parameter; resolve the expected sink from the stream under test |

Commit `f7d9e2066` carries those repairs. Three test files were also split to
restore cohesion where a large unrelated test block had dragged `LCOM4` down:

- `test_public_api.py` 779 → 564 lines, with the capture-policy cases moved to
  the new `test_capture_policy_surface.py` (135 lines).
- `test_stream_drain.py` 673 lines, with the cancelled-absorb case moved to the
  new `test_stream_drain_cancelled_absorb.py` (102 lines).
- `test_command_result_field_order.py` (113 lines) holds the public positional
  contract of `CommandResult`, which had been one of the blocks pulling
  `test_public_api.py` apart.

Each extraction was confirmed by falsification rather than intuition: removing
the candidate block and re-running `cs review` showed which one actually
cleared the finding. `test_pipeline_stream_routing.py` was briefly created as a
"duplicate" of `test_pipeline_config.py` and deleted again — the finding was on
the existing file, so a new file would have changed nothing.

`cs review` now reports 10.0 for every touched file.
`test_bounded_encoding_guard.py` also collapsed two duplicated refusal tests
into one parametrized case over `("stdout", "stderr")`, since both streams must
be refused identically.

**Seven-gate run at `f7d9e2066`.** All seven exited 0, sequentially, with a
provenance header and trailer in each log:

| Gate                | Elapsed | Closing evidence                                             |
| ------------------- | ------- | ------------------------------------------------------------ |
| `make check-fmt`    | 3s      | `1015 files already formatted`, `All checks passed!`         |
| `make markdownlint` | 48s     | `Summary: 0 issues in 0 files` (95 files)                    |
| `make nixie`        | 1s      | `All diagrams validated successfully!`                       |
| `make duplication`  | 2s      | `duplication gate passed; 68 allowed by reasoned exceptions` |
| `make typecheck`    | 3s      | `All checks passed!` on both passes                          |
| `make lint`         | 465s    | pylint 10.00/10, Clippy `-D warnings`, Whitaker, spelling    |
| `make test`         | 1629s   | 5425 passed, 113 skipped, 0 failed; nextest 167/167          |

The pytest totals are parsed from the log's 22 summary lines rather than
hand-counted. The Rust stage reported nextest `167 tests run: 167 passed` and
three doctest binaries all `ok` (`0 passed; 0 failed`, twice, and
`0 passed; 0 failed; 3 ignored`).

**Residual CodeScene findings, repaired after that run.**
`cs delta 85f91f6ec f7d9e2066` still reported two advisory `Large Method`
findings, both at 71 lines against a threshold of 70:

- `SafeCmd.lines` in `cuprum/sh/safe_cmd.py` — the three pre-flight refusals
  (allowlist, redirected stdout, bounded encoding) moved into
  `_reject_line_iteration_conflicts` in `cuprum/sh/line_options.py`, which is
  where the other line-iteration pre-flight rules already live. The moved
  helper is re-exported through `safe_cmd` like its siblings.
- `_build_subprocess_execution` in `cuprum/_command_execution_config.py` — the
  bounded-encoding refusal moved into `_reject_contested_captures`, a named
  step rather than an anonymous block between two other refusals.

Both files then re-measured 10.0 under `cs review`. Because the docstring is
counted by the rule, the repair had to move statements out of the method; no
documentation was trimmed to game the metric.

Date/Author: 2026-10-19, implementation agent.

## Sixth integration: the merge of `origin/main` and the ENOSPC abort

`origin/main` advanced twice while the CodeScene repairs were in flight
(`85f91f6ec` → `87d38aff8` → `ed45afde4`). The final head, `ed45afde4`
("Separate tar and rsync operands from options"), was merged cleanly into the
candidate as `d6ef141de`; the upstream side touched only
`cuprum/builders/{rsync,tar}.py`, builder tests and features, and three docs
files, so the merge introduced no packaging or capture change.

**Gate run at `d6ef141de`.** Six of the seven gates exited 0 sequentially
against that frozen head — `check-fmt` 2s, `markdownlint` 30s, `nixie` 3s,
`duplication` 0s, `typecheck` 2s, and `lint` 641s. `make test` was started
immediately afterwards and did not finish.

**What happened to `make test`.** The log holds 22 fully green pytest sessions
and then stops mid-way through the 23rd. The last lines are not a pytest
failure report: they are a traceback raised inside
`config.hook.pytest_sessionfinish`, cut off mid-frame, and the recipe's own
trailer line was never written, so the shell never regained control to record
an exit status. The background task's output file was zero bytes. That shape —
truncated log, no trailer, no task output — is a killed process tree rather
than a test outcome.

The host journal supplies the cause: at `2026-10-19T15:53:00` the `lody` daemon
logged `ENOSPC: no space left on device, write`, and file logging did not
resume until 15:55:01 (dropping 962 lines meanwhile). The truncated gate log's
last write is stamped 15:53:21. At that moment `/` held roughly 37 GB free (98
% used) with swap exhausted; by 16:06 the same filesystem held 193 GB free,
which is other agents' builds releasing space. The abort was host disk
exhaustion, not a defect in this branch.

The truncated log is preserved as
`/tmp/test-cuprum-rebase-issue443-onto-f359f2ec4.enospc-1553.out`. The four
tests that had reported `FAILED`/`ERROR` before the traceback were all in
`tests/test_py_typed_marker.py`, which builds real wheels and sdists via `uv`
and `maturin`; the build inputs were untouched by the merge. Re-running that
file alone at the same head after space returned gave `4 passed in 390.33s`.

The lesson worth carrying: a gate that stops without writing its own trailer is
not evidence of a pass or a fail. Check the trailer first, then the host
journal, before reading any pytest line as a verdict.

Date/Author: 2026-10-19, implementation agent.

## Completeness assessment on the published head

The ExecPlan assessment for `pb635-20261019-completeness-01` was requested
against base `ed45afde4` and head `219d6fa20`. CodeRabbit's substantive reply
(`#issuecomment-6272028914`, updated 2026-10-19T16:29:21Z) begins:

> Treat the implementation as substantially complete, but not fully correct
> against the ExecPlan. Address the two failure-path defects below before
> declaring O4 and O5 fully discharged. Also close the narrower O1 verification
> gap.

It raised two defects and one verification gap. Each was checked against the
source before repair; all three were confirmed real.

**Defect 1 — O5, a raising or cancelled session close strands spill files.**
`_reconcile_pipeline_run_failure` guarded only the observe-task drain with
`finally`; the delivery-worker close, the presentation-session close, and the
terminal events ran in the `try` above it. `_shielded_cleanup` completes its
operation and then *re-raises* the caller's cancellation, so a cancellation
landing during a bounded session close aborted the teardown before
`_cleanup_capture_sinks` ran. A session `close` that raised did the same. Both
are exactly the paths where the run is already in trouble and its spill files
have no result left to name them. The same shape was present in
`_finalize_pipeline_timeout` and `_finalize_pipeline_stage_result_failure`:
their existing `finally` enclosed only the drain, not the closes above it. All
three finalizers now reach the capture release from a `finally` that encloses
every preceding teardown step, preserving the order consumers-settle
before-capture-files-close. The two composed cleanups became one
`_settle_observe_tasks(observers, spawn, cause)`: with the `finally` moved out
to the three finalizers, their bodies were the same drain-then-release pair
differing only in the error object, and the duplication gate reported exactly
that pair as a new family. Collapsing them removed the family outright, so no
allow entry was added. The two direct-call cases in
`test_pipeline_cleanup_finally.py` became a third parameterization of the
finalizer-level drain case, which now drives all three paths.

**Defect 2 — O4, a failed append hides accepted-but-unflushed bytes.**
`_append` deliberately does not flush per chunk; `_flush_before_reading` is the
single flush before a byte-mode read-back, and `_read_back` opens the path on a
separate descriptor, so buffered writes are invisible to it. The guard
`if file is None or self._failed is not None: return` skipped that flush
whenever *any* storage failure had been recorded — including a failed append,
which leaves every previously accepted write sitting in the `BufferedWriter`.
The retained capture therefore stopped at the last successful flush rather than
at the last accepted write, while the module docstring and the users' guide
promised the prefix "that reached the file". The early return is now only for
`file is None`; a flush failure is reported under `storage_flush_failed` only
when no earlier failure already explains the capture, so the append's own event
stays the one the caller reads. The rewrite is net zero lines: the module sits
at exactly the 400-line ceiling.

**Gap — O1, byte mode lacked an arbitrary-binary property.** The retention
properties generated text and fed it to the accumulator as one chunk. That can
never witness two things the byte contract is about: that byte mode leaves
arbitrary bytes untouched (a text property only ever produces UTF-8 a decoder
would accept), and that the counts and the retained tail are measured against
the concatenated stream rather than per read. A new property generates
`st.binary` payloads and an arbitrary cut list, splits the stream at those
cuts, and asserts the *exact* tail `payload[-max_bytes:]`,
`total_bytes == len(payload)`, `dropped_bytes == max(0, len - bound)`, the
`dropped + retained == total` lemma, and `truncated` as exactly the over-budget
case. It publishes `over-budget`/`within-budget`, `binary`/`ascii`, and
`chunked`/`single-read` through `hypothesis.event`. The byte-mode sink builder
was promoted to `_bounded_capture_support._byte_sink` and the copy in
`test_bounded_capture_edges.py` removed, so the two suites share one config.

**Mutation probes.** Each new test was checked against a deliberate regression,
then the file restored from a `cp` copy (never `git checkout --`):

- Restoring `or self._failed is not None` in `_flush_before_reading` fails the
  new spill test with `got b'buffered prefix '`.
- Flattening the timeout and stage-result finalizers fails three cases (the
  raising close on both paths, and the cancellation case).
- Pointing either finalizer's `finally` at a drain-only cleanup, so the
  release is skipped, fails that path's raising-close case.
- Turning the run-failure outer `try/finally` into `if True:` fails the
  run-failure raising-close case.

**First-run failures in the new test code, and their lessons.** The property
module first failed for two self-inflicted reasons, both caught before any gate
ran. `_partition` paired boundaries with
`zip(boundaries, boundaries[1:], strict=True)`; the two iterables are
intentionally offset by one so `strict` can never hold — `itertools.pairwise`
states the adjacency instead. And the decorator emitted before
`test_no_bound_splits_a_code_point` named the binary strategy where the text
one belongs, so a `str` parameter received `b''`. A third round found ruff's
ASYNC240 on `tmp_path.iterdir()` inside an async test body; the read moved into
a synchronous `_entries` helper rather than a suppression, because a test-owned
temporary directory does not warrant an async path library.

Date/Author: 2026-10-19, implementation agent.

## Review follow-up on the published head

The candidate-bound assessment for repair head `7106bb525` confirmed the O4
buffered-write flush repair, O5 capture release on teardown failures, and the
O1 arbitrary-binary property. It identified one remaining overstatement: byte
read-back includes previously accepted bytes only if final flush and read-back
succeed. The users' guide and this design document now state that condition;
the earlier write-failure event remains authoritative if the final flush also
fails.

The inline review findings on published head `f926d12c` were checked against
the source and addressed. The changes correct three implementation comments and
docstrings, make the successful test coroutine yield, fix the roadmap ordinal
and migration link, clarify the resolved capture policy required for
redirection, and preserve forced capture in `SafeCmd.start()` when a stream's
field is `False`. New tests cover that handle contract, capture-observer scope
and failure behaviour, and propagated spill-file read errors. The developers'
guide now describes the accumulator and drain ownership that the code uses.

The review also suggested a filesystem capability abstraction and a built-in
metrics or tracing adapter. Neither is required by issue #443: its public
contract deliberately accepts a directory and reports the spill path, while its
opt-in `CaptureEvent` channel supplies the closed-set facts a caller can
forward to a metrics backend. These broader design suggestions remain outside
the delivered interface. The first fresh gate run passed six gates and found
three lint issues; the next run caught one redundant import and one Oxford
spelling error. Both were corrected before the final candidate certification.

Date/Author: 2026-10-20, implementation agent.

## Review follow-up on the published head `67c106e`

The focused CodeRabbit assessment at `6ee5a4b` accepted all five pre-merge rows
as closed, distinguishing implemented repairs from withdrawn interface and
telemetry requirements. It also noted that its assessment did not update the
displayed walkthrough table. The current API keeps the intended boundary:
`CapturedStream.read_text()` is a synchronous convenience read, documents its
blocking behaviour and propagated filesystem and codec errors, and does not add
a storage adapter or a `Result` wrapper. The design document now describes the
current `CaptureSink` and `_CapturedDrain` lifecycle rather than the removed
buffer-based path.

The hosted CodeScene check at `67c106e` reported `_with_capture` at the
complex-method threshold. The setup now delegates redirection refusal,
per-stream setting normalization and capture-option assembly to focused helpers.
`cs review` scored the touched file 10.0, and `cs delta` found no new health
issue. The initial gate run for that refactor caught a return annotation that
omitted the accepted `True` setting and lint findings from the helper's Boolean
parameter and relocated exception documentation. The setting now preserves the
declared `CapturePolicy | bool | None` type, is keyword-only, and the
`ValueError` contract belongs to the helper that raises it. The existing
read-back suite already covers both missing and unreadable files, so no
duplicate missing-file case was retained.

The corrected four-file candidate passed all seven local gates sequentially
with base `ed45afde4bde458da0677e635186b385570bcde2`, HEAD
`67c106e40fbaf750fed778a7a373ee7c021b027b`, and binary diff SHA-256
`b3c2c3ee3879c80c273e72c26394fdc457c8ba348c3dbd1c4e92dadb563c4334`. Logs are
under `/tmp/` with the `repair-b3c2c3ee` suffix. This evidence applies to that
candidate content. At the time this record was written, the final ExecPlan edit
still required its Markdown gates.

The five-file snapshot before this certification note passed all seven local
gates sequentially. It used HEAD `67c106e40fbaf750fed778a7a373ee7c021b027b`,
base `ed45afde4bde458da0677e635186b385570bcde2`, and diff SHA-256
`4fc2dd7d480e58eddb12d08174eafc55027e6d318365dbc38667d722cf64d782`. The
candidate-bound logs are under `/tmp/` with the `frozen-4fc2dd7d` suffix. Its
test gate reported 4,063 passed and 93 skipped in the main suite, 843 CI tests,
116 boundary tests plus the boundary shards, 173 behaviour tests, 167 passing
Rust nextest tests, and three ignored doctests with none failed.

Date/Author: 2026-10-20, implementation agent.

## Review follow-up on the O5 retention tests

The candidate completeness assessment found that the kept-spill tests did not
pin a completed run's full contents for both success and non-zero exit, or
external cancellation after ownership promotion. The new
`test_capture_spill_retention.py` covers those routes, and also keeps the
timeout prefix case beside them. Its cancellation case waits for the
`SPILLED_TO_FILE` event before cancelling, then checks that the public run
raises `CancelledError` and that the caller-owned file contains exactly the
bytes reported at promotion. The cleanup suite continues to cover
`keep_file=False` removal and provisional-spill cancellation.

The focused cleanup and retention suites passed 12 tests. The first fresh gate
sequence found two test-only lint issues: `_SpillWitness` was imported at
runtime despite being annotation-only, and the spill-byte guard combined two
assertions. The import now lives in `TYPE_CHECKING`, and the byte-count checks
are separate. This changes test coverage only; it changes no runtime behaviour
or public API. The full gates must pass on the combined tree before commit.

Date/Author: 2026-10-20, implementation agent.

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
- The coding plan delivered with the task, which supplies the four-task
  decomposition recorded under `Plan of work`. The original text was not
  committed; `Plan of work` and `Decision log` are its durable record.
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
ISSUE-443-AC-unicode-boundary     -> EP-T2 -> cuprum/unittests/test_bounded_capture_properties.py
                                             -> cuprum/unittests/test_bounded_capture_edges.py
ISSUE-443-AC-deterministic-cleanup-> EP-T3 -> cuprum/unittests/test_capture_spill_cleanup.py
ISSUE-443-AC-default-preserved    -> EP-T1 -> cuprum/unittests/test_public_api.py
```

## Verification plan

Obligations introduced or preserved by this change.

**O1 — Bounded tail retains a suffix and never splits a code point.**
Invariant, stated in two parts because they hold over different inputs.
*Byte-exact:* for any byte stream `S` and any `max_bytes = n > 0`, the retained
bytes equal the last `m` bytes of `S` for some `m` in `[0, n]`, and the union
of retained and dropped bytes is exactly `S` in order; in byte mode this is the
whole obligation. *Decodable suffix:* in text mode, for any stream valid under
a supported encoding (the ones `BoundedCapture` accepts, listed in its
docstring), the rendered payload is a suffix of `S`'s decoded text — the
retained bytes start on a code-point boundary and, once trimmed, can be read by
a strict decoder. Invalid input under a supported encoding, and the multi-byte
East Asian codecs that `BoundedCapture` refuses, are outside this obligation.
Lemma: `dropped_bytes + len(retained) == total_bytes`. Method: property test
with Hypothesis over generated byte strings and bounds, plus parameterized
cases for the boundaries `n = 1`, `n` exactly equal to the stream length,
`n = len(S) - 1`, and `n > len(S)`. Artefact:
`cuprum/unittests/test_bounded_capture_properties.py` (the properties) and
`cuprum/unittests/test_bounded_capture_edges.py` (the named boundaries).
Evidence: hypothesis reports `Falsifying example` before the accumulator
handles multi-byte input; all cases pass after. Non-vacuity: generators must
reach multi-byte UTF-8 (assert a classification counter that at least one
generated case contains a code point of >1 byte) and must exercise both the
"under budget" and "over budget" classes; the negative control is a
deliberately off-by-one bound (`max_bytes = n + 1`) which the `dropped_bytes`
lemma rejects.

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
`keep_file=True` the file survives holding every byte that reached it before
the run stopped — the whole stream on the two returning routes, and the
received prefix on timeout and cancellation, which is the partial output O8
describes. One cancellation is excluded, and it is the one that lands before
the initial spill's prefix is written: the handle is still the provisional one
that `_publish_spill` created with `keep=False`, so cleanup removes the file
even under `keep_file=True`. That is deliberate — until the prefix lands there
is no caller-owned file — and it is recorded in the ADR addendum. Method:
end-to-end cleanup tests over the four outcomes, asserting `Path.exists()`
afterwards, plus retained-file tests for completed, timeout, and post-promotion
cancellation routes. Artefacts:
`cuprum/unittests/test_capture_spill_cleanup.py` and
`cuprum/unittests/test_capture_spill_retention.py`. Evidence: the timeout and
cancellation cases fail before the cleanup is registered through
`_shielded_cleanup`. Non-vacuity: each case first asserts the file *existed*
during the run (via the reported metadata path), so "not there afterwards"
cannot be satisfied by never having written it.

**O6 — A bound never reaches the relay.** Invariant: in a pipeline where the
final stage's stdout is bounded, the stage after the producer reads the
producer's complete stream. Method: behavioural test running a producer stage
under `BoundedCapture` with a reporter stage that announces how many bytes it
read. Artefact: `tests/behaviour/test_bounded_capture.py` (scenario "A bound
never reaches the relay") and `tests/features/bounded_capture.feature`.
Evidence: `result.stdout == _ANNOUNCEMENT` — the reporter's own output, which
is the length it read — together with `relayed.dropped_bytes == 0` on the
reporter's record. The producer's stdout has no record of its own: interior
stdout is relayed rather than captured, so only the bound's effect on what the
consumer read is observable. Non-vacuity: the payload is strictly larger than
the bound, so a leaking bound would shorten the reported count.

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
that moment — text for the bounded case, and for the file-backed case a
readable spill file, which requires `keep_file=True` because the default
removes the file on every outcome (O5). Method: parameterized test with a child
that writes a known prefix then stalls past a short timeout, asserting the
retained payload. Artefact: `cuprum/unittests/test_capture_timeout.py`.
Evidence: the assertion fails if finalization only runs on the clean-EOF path.
Non-vacuity: the child writes a non-empty prefix before stalling, and the test
asserts that prefix specifically rather than mere non-emptiness.

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
Remaining at M3: the end-to-end halves, which are two distinct cases and must
not be stated as one. A run left at the defaults (`RunOutputOptions()`, no
capture policy named) resolves both streams to `FullCapture()` and so ends with
two `full`/`memory` records *and* complete `stdout`/`stderr` text; that is the
default this change promised to preserve. A run with `capture=False` ends with
`captures == ()` and `stdout`/`stderr` both `None`, because no stream was
captured at all. The empty-tuple case is therefore the opt-out, not the default
— conflating the two would make O9's "the default is unchanged" claim
unfalsifiable. Non-vacuity: the test constructs a seven-positional
`CommandResult` and reads the bound value back, so a shifted slot is detected
rather than assumed.

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
`CapturePolicy` and `CaptureStreamName` type aliases, validation in
`__post_init__` raising `ValueError` for non-positive bounds (with
`FileBackedCapture` also normalizing a `str` directory to a `Path`), and a
frozen slotted `CapturedStream` metadata record with a `read_text()` accessor.
Add `DEFAULT_CAPTURE_SPILL_THRESHOLD_BYTES` to `cuprum/_constants.py`. Add
`capture_stdout`/`capture_stderr` to `RunOutputOptions` — declared `kw_only` so
the pinned first six positional slots do not move — resolve them from `capture`
in `__post_init__`, and expose `resolved_capture`. Add the trailing `kw_only`
`captures` field to `CommandResult` and a `captures` property to
`PipelineResult`. Export the new names from `cuprum/sh/__init__.py` and
`cuprum/__init__.py`. Nothing consumes the policies yet, so M1 is a pure
addition and every existing test passes unchanged.

**EP-M2 — Accumulators, events, and the drain tap (Task 2).** Add the three
accumulators behind one interface — a full accumulator, a bounded-tail
accumulator, and a spill accumulator — each offering "absorb this chunk" and
"finalize" (public value plus metadata), plus an async cleanup for the spill
file. They are split across `_capture_types.py`, `_capture_accumulators.py`,
`_capture_file.py`, and `_capture_cleanup.py` as recorded under
[Interfaces and dependencies](#interfaces-and-dependencies), rather than the
single module this plan first sketched. Add `cuprum/capture_events.py` (closed
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
— the accumulator tests, which import `cuprum._capture_accumulators` — the
module's public interface is written first with a body that raises
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
`cuprum/unittests/test_bounded_capture_properties.py`,
`test_bounded_capture_seam.py`, `test_bounded_capture_edges.py`, and
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
`FileBackedCapture` in byte-exact mode reads the whole spill back, so byte mode
does not carry text mode's bounded-memory guarantee. Compatibility decision:
`<none>`.

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

The evidence this plan expected — the failing-then-passing accumulator test and
the cancellation-path spill-cleanup assertions — is recorded per milestone in
`Progress`, with the non-vacuity measurements and mutation checks in
`Surprises & discoveries`. The capture-work review rounds and their fixes are
recorded in `Decision log`.

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

## Revision note (2026-10-20)

Record the O5 kept-file end-to-end coverage added after review, point its
verification artefact at the new retention suite, and document the two
test-only lint fixes. The runtime contract, public API, and remaining feature
milestones are unchanged.
