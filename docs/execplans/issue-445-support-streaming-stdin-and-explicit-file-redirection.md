# Support streaming stdin and explicit file redirection (`#445`)

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT

## Purpose / big picture

Today a cuprum caller who wants to feed a child process more input than fits
comfortably in memory has no option: `StdinInput` resolves one complete `str`/
`bytes` payload before the child is spawned, and the spawn configuration that
cuprum builds chooses only between inherited stdin, a library-owned input pipe,
and internally selected output pipes or `/dev/null`. There is no public
streaming input source, and no public way to say "the child's stdout goes to
*this* file" or "the child's stderr goes to *this* already-open descriptor".

After this change a caller can write:

```python
from cuprum import StdinStream, StdioTarget

run = builder("consumer.py").run(
    stdin=StdinStream(async_chunks()),
    output=RunOutputOptions(stdout=StdioTarget.path(Path("out.log"))),
)
```

and mean two new things. First, the producer's chunks are pulled one at a time,
written to the child's stdin, and drained before the next chunk is pulled, so
peak memory is bounded by one chunk rather than by the payload. Second, the
child's stdout is bound to a file cuprum opened for the run and closed as soon
as the child was spawned — the child keeps the descriptor for its lifetime,
cuprum keeps ownership of closing it, and the caller never sees a leak.

The existing `stdin=StdinInput(...)` payload API and the inherited-stdin
default (the current `stdin=None`) behave exactly as they do today; this is an
additive widening, not a replacement.

Success is observable three ways. A child that reads 64 MiB slowly while our
producer yields 4 KiB chunks completes without the parent ever holding more
than one chunk, and cuprum's own peak resident set stays flat. A run whose
stdout is redirected to a file leaves that file with the child's exact bytes
and the file descriptor closed, both on `exit_code == 0` and on a non-zero
exit. And a producer that raises mid-stream terminates the child, surfaces a
`StdinSourceError`, and leaves no writer task, no pipe, and no orphan process
behind.

## Constraints

Hard invariants that must hold throughout implementation. Violation requires
escalation, not a workaround.

- **`SafeCmd` only.** Streaming stdin and stdio targets are added to
  `SafeCmd.run`, `SafeCmd.run_sync`, and `SafeCmd.lines`, and to
  `RunOutputOptions`. `Pipeline` stage wiring is unchanged: a stage still
  inherits the library-owned pipe that `Pipeline` builds. A caller who wants a
  redirected pipeline stage does not get one from this change.
- **Standard streams only.** Only `stdin`, `stdout`, and `stderr` may be
  redirected. Passing arbitrary extra descriptors to the child is explicitly
  out of scope, as the issue allows.
- **No shell redirection, no helper commands.** `>`, `<`, and `|` remain shell
  syntax that `SafeCmd` takes literally. Nothing in this change adds an
  allowlisted helper program to the test catalogues to stand in for a file, a
  FIFO, or a descriptor.
- **Existing payload API and inherited default are preserved.** `StdinInput`
  keeps its constructor, its `resolve` method, and its `__post_init__`
  validation. `stdin=None` still means "inherit the parent's stdin". The
  `_RunKwargs` test TypedDict keeps accepting `stdin: StdinInput | None` in
  addition to the new form.
- **Only library-owned resources are closed.** A path target opens the file,
  hands the descriptor to the child, and closes cuprum's copy in a `finally`
  immediately after spawn. A borrowed descriptor or file object is never
  closed, and a borrowed file object is flushed before spawn so buffered
  caller-side bytes are visible to the child.
- **A stream that is not a pipe gets no consumer and no writer.** The library
  must not attach a `StreamReader` to a descriptor it does not own, and must
  not spawn a stdin writer for a stream it did not create.
- **No behavioural change when the new arguments are absent.** With
  `stdin=StdinInput(...)` or `stdin=None` and no stdio targets, observable
  output, exit codes, event streams, and the returned results are identical to
  the current release.
- **`_EventDetails` is closed.** The observation vocabulary is the existing
  `ExecEvent` field set. Streaming reports progress as one `stdin` event per
  written chunk, each carrying that chunk's `byte_count`, and at most one
  `stdin_error` event for an early child-side close. No aggregate counter field
  may be added.
- **Interior modules never runtime-import `cuprum.sh`.** They use
  `if typ.TYPE_CHECKING` plus the `_subprocess_context._sh_module()` lazy shim.
  `cuprum/sh/safe_cmd.py` continues to omit
  `from __future__ import annotations` because its public signatures are
  resolved by `typing.get_type_hints`.
- **Oxford spelling, 400-line module ceiling, frozen slotted dataclasses.**
  ADR-009 and the existing house style apply to every new module.

## Tolerances (exception triggers)

Thresholds that trigger escalation when breached. These bound autonomous
action; they are not quality criteria.

- Scope: if implementation requires editing more than 24 tracked files or more
  than 1400 net lines, stop and escalate.
- Interface: if `StdinInput`, `RunOutputOptions`' existing field names, or any
  `SafeCmd` parameter other than `stdin` must change shape, stop and escalate.
  Widening `stdin` to a union and adding new *optional, keyword-only* fields is
  the approved change and does not trip this trigger.
- Dependencies: if a new runtime dependency is required, stop and escalate.
  The intended implementation uses only the standard library.
- Iterations: if a single failing gate persists after three focused fix
  attempts, stop and escalate with the captured log.
- Time: if any one milestone exceeds four hours of wall-clock work, stop and
  report progress before continuing.
- Ambiguity: if the descriptor-passing semantics of a platform turn out to
  differ from the model in `Verification plan`'s axioms, stop, record the
  finding in `Surprises & discoveries`, and present options rather than
  guessing.

## Risks

Known uncertainties that might affect the plan. Each notes severity,
likelihood, and mitigation.

- Risk: `subprocess.Popen` retains `self.stdin`/`self.stdout`/`self.stderr`
  only for `asyncio.subprocess.PIPE`; a raw integer or a file object is passed
  to the child and never wrapped. If cuprum's pipe-connection code attaches a
  `StreamReader` to a pinned descriptor anyway, the run will hang or corrupt.
  Severity: high. Likelihood: medium. Mitigation: represent pipe-ness
  separately from the descriptor value (a `pipes: frozenset[str]` field or a
  `PIPE_SENTINEL`) so `_Wait4Process.connect_pipes()` can skip non-pipe
  streams; prove the skip with a test that redirects stdout to a file while
  stderr stays captured.
- Risk: closing the parent's copy of a path descriptor immediately after spawn
  is only safe once the child has inherited it, and `Popen` returns after the
  child is forked and `exec`ed; a change to spawn ordering could close too
  early and give the child a stale descriptor. Severity: high. Likelihood: low.
  Mitigation: close inside a `finally` *after* the spawn call returns, and
  assert the child's output is byte-exact in the redirect tests.
- Risk: a pull-after-drain producer can deadlock when the consumer is not being
  drained — for example when the child writes a lot to stdout while the parent
  is inside `await drain()` on stdin. Severity: medium. Likelihood: medium.
  Mitigation: producers are pulled only after `drain()` returns, stdout/stderr
  consumers run as independent tasks for the whole run, and a large-payload
  test exercises both directions at once.
- Risk: the new `lines()` path and the two execution paths drift, so streaming
  works under `run()` but leaks under `lines()`. Severity: medium. Likelihood:
  medium. Mitigation: parameterize every streaming test over the strategy
  fixture (`run()`/`run_sync()`) plus a dedicated `lines()` finalization test.
- Risk: producer cancellation interacts badly with `_shielded_cleanup`, leaking
  a writer task that outlives the event loop. Severity: medium. Likelihood:
  medium. Mitigation: every exit path calls iterator `aclose()` when available,
  then closes the pipe and awaits `wait_closed()`; cancellation tests assert no
  pending tasks remain (the existing `asyncio.all_tasks()` sweep pattern).
- Risk: adding fields to `RunOutputOptions` trips `mdtablefix`, `typos.toml`
  regeneration, or the `test_public_api.py` positional-slot pins. Severity:
  low. Likelihood: medium. Mitigation: new fields are keyword-only additions
  placed after existing fields; run `make markdownlint` after any doc edit and
  commit the regenerated `typos.toml` as its own commit.

## Progress

- [x] (2026-09-27 00:35Z) Reconnaissance complete: every module named in this
  plan read; CPython `Popen` stdio semantics verified against source; absence
  of interior runtime `cuprum.sh` imports confirmed by grep.
- [x] (2026-09-27 01:10Z) ExecPlan authored.
- [x] (2026-09-27 05:40Z) EP-M1 complete. `StdinStream`, `StdinSourceError`,
  and the `type StdinSource` alias live in `cuprum/sh/execution.py`;
  `StdioTarget` and `_validate_stdio_targets` live in `cuprum/sh/stdio.py`
  (since the 04:45Z relocation below; `cuprum/sh/output.py` re-exports them);
  `stdin=` is widened on `SafeCmd.run`/`run_sync`/`lines`; `RunOutputOptions`
  carries `stdout`/`stderr` targets. All four names are exported from
  `cuprum.sh` and `cuprum`. Red: 10 failed / 14 passed. Green: 24 passed. Wider
  set (stdin, output, run, lines, streams, timeout, context, property,
  early-close, pipeline-output): 169 passed, 12 skipped.
- [x] (2026-09-27 04:45Z) Module-size plateau, first pass. Both
  branch-introduced `too-many-lines` findings are refactored rather than
  suppressed: `cuprum/sh/stdio.py` takes the stdio target vocabulary out of
  `cuprum/sh/output.py` (584 → 349 lines), and `cuprum/sh/pipeline.py` takes
  `Pipeline` out of `cuprum/sh/safe_cmd.py` (450 → 325 lines). `pylint`'s
  `too-many-lines` is in the `enable = [...]` list, so it cannot be annotated
  away, and no production module on `origin/main` exceeds the 400-line ceiling
  — both overruns were introduced by this branch. Commits `cbf8c190`,
  `9532dfd4`, `81b2ef2c`. The maturin wheel snapshot is regenerated for the two
  new modules.
- [x] (2026-09-27 09:05Z) Module-size plateau, second pass, and the gate sweep
  at HEAD `3037eb63` that found it. `cuprum/_subprocess_stdin.py` had grown to
  451 lines (119 on `origin/main`, 371 at `3037eb63`) as the streaming writer
  and its encoder landed, re-crossing the ceiling the 04:45Z entry above had
  cleared for *other* modules. The producer path moves to
  `cuprum/_subprocess_stdin_stream.py` (328 lines), leaving
  `_subprocess_stdin.py` at 164 — the seam is the one the module's own
  docstring already drew, between a one-shot payload and a pulled producer.
  Routing: `_subprocess_stdin` keeps the ADR-007 roster (`_emit_stdin_error`,
  `_write_stdin`, `_close_stdin`, `_cancel_stdin_writer`, the `cuprum.stdin`
  logger) plus `_spawn_stdin_writer`, which stays the single dispatcher both
  kinds of source are started from; the streaming module imports the pipe
  primitives at module scope and `_spawn_stdin_writer` imports
  `_write_stdin_stream` inside the function body, because a module-scope import
  in both directions would close a load-time cycle. The same sweep also caught a
  `spelling` regression in the new test module: a British-spelled variant of
  "recognize" that the gate's own correction table (`typos.toml`) enumerates
  with its American fix. ADR-007 gains a 2026-09-27 addendum, and the module
  rosters in `docs/cuprum-design.md` and `docs/developers-guide.md` name the
  new module.
- [x] (2026-09-27 04:50Z) `make lint` sub-checks all observed passing, on a
  single HEAD (81b2ef2c): `ruff`, `interrogate`, `pylint`, `df12-pylint`,
  `ambrleaks`, `skylos`, `clippy`, `whitaker`, `spelling`, `yamllint`,
  `actionlint`. Two abort-driven gaps were closed by running the aborted stages
  directly rather than re-running the whole target: `spelling` (after the
  rejected-token reword) and `github-actions-lint` (a standalone run, because
  the in-target `actionlint` invocation stalls locally in a shellcheck stdin
  deadlock that CI does not hit). The full-target run reached `spelling` and
  aborted there, so `github-actions-lint` is evidenced by its own run, not by
  the aggregate.
- [x] (2026-09-27 07:20Z) First CodeRabbit review round, at HEAD 3037eb63
  (`review_completed`, 4 findings, bound to that revision). One `major` was
  real and is fixed: the streaming writer read its encoder settings with
  `getattr(process, "encoding", "utf-8")`, but `asyncio.subprocess.Process` has
  no such attribute — cuprum passes raw descriptors, not text-mode file objects
  — so both fallbacks always won and a streaming `str` chunk was always UTF-8/
  `replace` regardless of `ExecutionContext`. The settings now travel in a
  `_StdinCodec` built from `execution.ctx`. The review's other three findings
  were doc-level: an over-claiming EPIPE docstring (fixed by adding the
  `errno.EPIPE` arm, which is the arm the docstring promised) and two stale
  ExecPlan references to the pre-relocation `cuprum/sh/output.py`.
- [x] (2026-09-27 14:20Z) Module-size plateau, third pass, and the round-2
  review fixes. Two more branch-introduced `too-many-lines` findings, both
  created by EP-M2's own additions: `cuprum/_subprocess_execution.py` reached
  523 lines and `cuprum/_subprocess_wait.py` 438, against 363 and 392 on
  `origin/main`. Cleared by two further extractions at cycle-safe seams:
  `cuprum/_subprocess_spawn.py` (~195 lines) takes the stdio mapping, the
  owned-descriptor open/close pair, and the spawn call, and
  `cuprum/_subprocess_deadline.py` (153 lines) takes the child-exit wait and
  its deadline. `_subprocess_execution.py` returns to 367 and
  `_subprocess_wait.py` to 327. Both moved names are re-exported from their
  original modules, so every monkeypatch seam and import path the suite relies
  on is unchanged; the 20-module focused suite is 185 passed. Remaining round-2
  scrub items also closed: the four test helpers that pass an
  `eof_grace_waiter` now annotate its parameter as `_ConsumerPair` rather than
  a non-optional `tuple[Task[str | None], Task[str | None]]` (the parameter is
  contravariant, so the narrower annotation was the rejected one), and
  `cuprum/_line_stream/drain.py` unpacks its `asyncio.gather` result rather
  than returning the `list` it produces as the declared `tuple`.
  `make typecheck` reports zero diagnostics. The maturin wheel snapshot is
  regenerated from a real wheel build, which also picked up the
  previously-unrecorded `cuprum/_stdio_plan.py` from the 04:45Z pass.
- [x] (2026-09-27 07:20Z) EP-M2 plateau committed as `77553d30`, on a fully
  green gate sweep. All seven targets ran under `scrutineer`, sequentially,
  each teed under `/tmp`: `check-fmt`, `lint`, `typecheck`, `markdownlint`,
  `nixie`, `spelling`, `test`. `test` printed nine pytest sessions all green
  (2507/638/2/116/4/123/12/21/22 passed), nextest `125 tests run: 125 passed`,
  and zero `FAILED`/`ERROR`/`panicked` in 3823 log lines. The one non-green
  observation was environmental, not a defect: `make lint` completed every
  Python and Rust stage and then stalled *inside* `actionlint`
  (`futex_wait_queue`, no children, ~549 s) — the known local shellcheck stdin
  deadlock; a standalone run with `shellcheck` off the `PATH` exited 0, which
  is the CI condition. Two secondary findings from the same sweep: the
  `ACTIONLINT` make variable cannot carry `-shellcheck=` (it is parsed through
  `ensure_tool` and the `TOOLS` list as a bare tool name, so the flag reads as
  a missing tool), and local `uv tool run --python pypy` resolves to PyPy
  **3.12.14**, not the 3.11 the earlier note in this plan assumed. `typos.toml`
  did *not* regenerate this time (SHA-256 byte-identical across the run), and
  the idle-heartbeat shared-sink keepalive test did not flake. The commit's
  tree hash is `dd9dfee5577c835eb9cde44b8e2633de94f86f4f`, recorded from
  `git write-tree` before the commit and re-read from `HEAD^{tree}` after it.
  That equality is deliberate: this sweep ran against the *staged* tree at
  `a1a0db9e` plus its delta, and a commit would otherwise invalidate it as a
  citation. The tree, not the revision, is what the gates observed, and the
  tree is unchanged.
- [x] (2026-09-27 18:05Z) EP-M3: the rendezvous. Producer failure during a run
  was reported as a slow child: the writer ran alongside the exit wait but
  nothing raced them, so a producer that died at once was only noticed when the
  child's own deadline expired, and the caller got `TimeoutExpired` about a
  failure already known. `_await_exit_or_writer_failure` now owns that
  decision, in a new `cuprum/_subprocess_rendezvous.py` (165 lines), because
  ending a run divides in three and "end because the input source died" is
  neither `_subprocess_deadline`'s business (the child's deadline) nor
  `_subprocess_wait`'s (the parent's task reconciliation). The resolution is
  deliberately narrow: only a writer that *failed* ends the run there; one that
  merely finishes first is the ordinary way a stream ends, and the run
  continues to the child's exit as before. No termination policy is restated —
  the exit wait is cancelled, and `_wait_for_exit_code`'s own cancellation
  handler already runs SIGTERM, `cancel_grace`, SIGKILL. The helper takes its
  exit wait already constructed, so the two test modules that patch
  `_wait_for_exit_code_within_timeout` by name in their *own* namespace keep
  both seams. Three call sites: the streamed run, the unstreamed run, and the
  line-stream coordinator. Red was observed, not assumed — the new
  `test_producer_failure_beats_the_timeout` failed with exactly the predicted
  signature (`TimeoutExpired ... timed out after 5 seconds`,
  `1 failed, 9 passed`) and passes at `elapsed < 2.0s` with the producer's own
  exception chained as `__cause__`. Two test-side shapes moved with it:
  `_failed_run` passes `stdin_task=None` to take the no-writer branch, and the
  timeout leak test is async-only and hand-driven because `asyncio.all_tasks()`
  needs a running loop — a census taken after `asyncio.run` returns is empty
  whatever the implementation did. Committed as `bd8ceb58`; focused suite 35
  passed; full `cuprum/unittests` 2516 passed, 63 skipped.
- [x] EP-M2: `_StdinPlan` replaces `stdin_data`; resolved stdio planning and
  spawn-time binding on both backends; descriptors opened before spawn and
  closed in `finally`; consumers and writers built only for piped streams.
- [x] EP-M3: pull-after-drain streaming stdin with bounded memory; producer
  failure wrapped in `StdinSourceError` and routed through the existing
  teardown; early child-side close recorded as a `stdin_error` observation;
  timeout and cancellation behaviour unchanged.
- [x] (2026-09-27 11:30Z) EP-M4 test artefacts, and three gate failures the
  first gate sweep caught in them. `test_safe_cmd_stdio_rules.py` (356 lines)
  covers the refusal boundary rather than the happy path: every rejection row
  is paired with the accepted near-miss sitting beside it, because a rule that
  refused everything would satisfy the rejection assertions on its own. The
  four pairings are the wrong-payload variant against the correctly-shaped one,
  `stdin` refusing a destination that `stdout` accepts, two owned paths against
  two borrowed descriptors on one stream, and `lines()` refusing a redirected
  stdout while an explicit pipe iterates. `test_stdin_property_based.py` gains
  Suite 4: generated `str`/`bytes` chunk lists through `StdinStream`, with the
  child hexing its stdin so the comparison is byte-perfect, and four forced
  examples reaching the empty-list and empty-chunk classes where a missing
  final encoder flush shows. The first gate sweep failed three of seven gates,
  all on the new work: `check-fmt` on this file (19 lines mdtablefix wanted
  reflowed), `lint` on 7 x PT018 composite assertions plus one D403
  lowercase-docstring, and `typecheck` on three deliberate-invalid arguments
  reaching typed parameters. All are fixed: the composite assertions split so
  each half carries its own diagnostic, `ty: ignore[invalid-argument-type]`
  marks the rows that pass a value the vocabulary does not define (the
  documented pattern at `test_benchmark_suite.py:154`), the parametrized kind
  is annotated `typ.Literal["pipe", "inherit"]` rather than `str`, and the
  docstring opens `Stdin`. Post-fix: module 22 passed, `python-lint` 0 (76 s),
  `check-fmt` 0.
- [ ] EP-M4 docs: user guide (streaming contract and the redirection ownership
  vocabulary), `cuprum-design.md` (the rendezvous roster entry), a new
  append-only ADR-007 addendum for the rendezvous, `roadmap.md`, and
  `CHANGELOG.md`.
- [ ] Push and open the draft PR (`(#445)` in the title, `Closes #445` in the
  summary, Lody session link under `## References`).

## Surprises & discoveries

- Observation: `subprocess.Popen._get_handles` maps `PIPE` to `os.pipe()`,
  `DEVNULL` to `_get_devnull()`, a raw `int` directly to the child-end
  descriptor, and anything else through `<obj>.fileno()`; `self.stdin`,
  `self.stdout`, and `self.stderr` are assigned only for the parent pipe ends,
  so raw descriptors and file objects leave those attributes `None` and are
  never closed by the parent. Evidence: CPython `subprocess.py` read directly
  this session; `asyncio.unix_events._UnixSubprocessTransport._start` passes
  the three values straight through to `Popen`. Impact: a `DirectProcessConfig`
  carrying only `int | None` cannot distinguish `asyncio.subprocess.PIPE` from
  a borrowed descriptor, so pipe-ness needs separate representation, and
  `connect_pipes()` must be gated on it.
- Observation: no interior module runtime-imports `cuprum.sh`; only
  `cuprum/__init__.py` and `if typ.TYPE_CHECKING` blocks do. Evidence:
  repository-wide grep for `from cuprum.sh` / `import cuprum.sh`. Impact: a
  `type` alias union such as `StdioTarget` is safe to define in
  `cuprum.sh.execution` and reference from interior modules behind
  `TYPE_CHECKING` without a cycle.
- Observation: `_EventDetails`' field set is exactly `ExecEvent`'s, so there is
  no aggregate byte counter to extend. Evidence: `cuprum/_pipeline_types.py`.
  Impact: streaming progress is reported as per-chunk `stdin` events with their
  own `byte_count`, not as a running total.
- Observation (EP-M1): a PEP 695 `type StdinSource = StdinInput | StdinStream`
  alias does **not** flatten when unioned. `StdinSource | None` has
  `typ.get_args(...) == (StdinSource, NoneType)`, and
  `(StdinSource | None) == (StdinInput | StdinStream | None)` is `False`.
  Evidence: a probe against the built module printed the alias repr and both
  argument tuples. Impact: `test_stdin_parameter_accepts_both_source_forms`
  checks `sh.StdinSource.__value__ == sh.StdinInput | sh.StdinStream` rather
  than comparing the annotated union against an inline expansion. Any future
  test that wants the flattened union must read `__value__`.
- Observation (EP-M1): `cuprum/sh/output.py` had no runtime `Path` import —
  `StdioTarget.path()` raised `NameError` under
  `from __future__ import annotations`, because the annotation form hid the
  dependency until the recipe ran. Evidence: 7
  `NameError: name 'Path' is not defined` failures in the first green run.
  Impact: `pathlib.Path` is now a genuine runtime import in that module; the
  annotations-first habit does not excuse a name the body uses.
- Observation (module split): `typing.get_type_hints` evaluates a quoted
  annotation against the *defining module's* namespace alone. Splitting
  `Pipeline` into its own module made `SafeCmd.__or__`'s `"SafeCmd | Pipeline"`
  return annotation unresolvable — it raised
  `NameError: name 'Pipeline' is not defined` — even though the identical
  annotation had resolved when both classes shared a module. Evidence: probe on
  the split tree, before and after binding the name. Impact:
  `cuprum/sh/safe_cmd.py` binds `Pipeline` with a module-level import at the
  *bottom* of the file, after `SafeCmd` is defined. A function-local import
  inside `__or__` is not sufficient, because it fires after introspection would
  have failed. This is the reason the import order looks wrong and is not.
- Observation (module split): a module-level import placed before the class
  definitions still deadlocks the cycle. `cuprum.sh.pipeline` imports `SafeCmd`
  at its own module scope, so binding `Pipeline` at the *top* of `safe_cmd.py`
  asks `pipeline` to import a `SafeCmd` that does not exist yet —
  `ImportError: cannot import name 'SafeCmd' from partially initialized module`.
  Evidence: the first attempt at the split. Impact: bottom placement is load
  bearing, not stylistic.
- Observation (gates): local `actionlint` (v1.7.12) stalls at the
  `github-actions-lint` recipe line because of a stdin pipe-buffer race in its
  shellcheck integration; this branch touches no `.github/` file. Evidence:
  `make github-actions-lint` exited 0 and printed both recipe echoes when the
  local shellcheck was absent from `PATH`, while the aggregate `make lint` run
  never reached the stage. Impact: the stage's pass is evidenced by a scoped
  standalone run; the aggregate target should not be trusted to exercise it on
  this host.

- Observation (CodeRabbit round 1): `asyncio.subprocess.Process` exposes no
  `encoding` or `errors` attribute, so a `getattr(process, "encoding", ...)`
  fallback is not a fallback — it is dead code that always wins. The streaming
  writer therefore ignored `ExecutionContext.encoding` and `errors` entirely: a
  `cp1252` caller received UTF-8 bytes, and `errors="strict"` silently degraded
  to `replace`. Evidence: with `encoding="cp1252"`, a streaming `str` chunk
  produced `e28093` where the equivalent `StdinInput` payload produced `96`;
  with `ascii`/`strict` the stream exited 0 while the payload raised
  `UnicodeEncodeError`. Impact: the codec now travels as a `_StdinCodec` read
  from `execution.ctx` at all three spawn sites, and
  `cuprum/unittests/test_safe_cmd_stdin_stream.py` pins both settings against
  the child's raw stdin bytes. The general lesson is that a defensive `getattr`
  default on an attribute a type does not have *hides* the bug rather than
  tolerating its absence.
- Observation (verification): the four encoding pins were checked for
  non-vacuity by reverting the writer to the pre-fix `getattr` encoder and
  re-running: all four failed
  (`the child should receive exactly the cp1252 bytes, not UTF-8` for the
  encoding pins, and a decode failure for the strict pins), then passed again
  on restore. Evidence: the revert/restore run in `/tmp`. Impact: the pins are
  load-bearing rather than merely satisfied.
- Observation (engine quirk): `except SomeError as exc if cond:` is a syntax
  error — `except` clauses accept no condition. The first attempt at the
  `errno.EPIPE` arm used that form and never compiled. Impact: the arm is an
  explicit `except OSError` with a guarded `raise ... from exc` inside, and the
  wrap is spelled at the handler (rather than inside a helper that raises on
  the caller's behalf) because the repository's `blind-except` rule requires the
  `raise` to be visible in the handler body.

- Observation (lints): `RUF029` (`unused-async`) flags an `async def` that
  yields but never awaits, so a minimal async generator in a test helper is
  rejected as if it were a mislabelled coroutine. Evidence: a two-function
  probe under the repo config flagged both the plain generator and the
  `__all__`-documented one, while the guarded one did not. Impact: the test
  helper carries an `await asyncio.sleep(0)` before its `yield`, matching the
  existing producer in `test_public_api.py` — which is also more faithful to a
  real producer, since a real one suspends between chunks. The added multi-line
  docstring then required a `Yields` section under `pydoclint` (one-line
  docstrings are exempt), so the two lints interact.

- Observation (module size): a *cleared* module ceiling is not a stable
  property of a branch, because a later milestone puts code back into the
  modules the earlier one trimmed. The 04:45Z plateau cleared the two overruns
  that existed then — `cuprum/sh/output.py` and `cuprum/sh/safe_cmd.py` — but
  fixing the CodeRabbit encoding finding threaded a codec through the streaming
  writer and pushed `cuprum/_subprocess_stdin.py` from 371 to 451 lines,
  re-crossing the same ceiling at commit `3037eb63` with no further refactor.
  The lesson is to re-measure the ceiling-bearing modules at every milestone
  boundary rather than trusting an earlier pass. Evidence: scrutineer's sweep
  quoted `origin/main` 119, `HEAD` 371, staged 451 for that one file. Impact:
  the producer path moves to `cuprum/_subprocess_stdin_stream.py` (328 lines)
  and `_subprocess_stdin.py` returns to 164; the 400-line ceiling is a `pylint`
  check the branch must hold at every milestone, not only at the plateau that
  first cleared it.

- Observation (module size, EP-M3): the ceiling was met again by the *fix*
  rather than the feature. Adding the rendezvous to
  `cuprum/_subprocess_wait.py` took it from 327 to 449 lines — 49 over — and
  the extraction that cleared it was itself forced by the same rule the
  milestone was obeying. Evidence: `wc -l` after the fix read 449 against the
  400-line `max-module-lines` ceiling in `pyproject.toml`. Impact:
  `cuprum/_subprocess_rendezvous.py` owns the race and `_subprocess_wait.py`
  returns to 336, re-exporting the moved name so the three call sites and the
  monkeypatch seams keep one import path. This is the third pass at the same
  ceiling (04:45Z, 09:05Z, 14:20Z were the earlier ones) and confirms the
  lesson above: a milestone that adds *any* behaviour to a module already near
  the line has to budget for the extraction, not just for the behaviour.

- Observation (docs coupling): the module roster is test-enforced, so a split is
  not a code-only change. `cuprum/unittests/test_async_timeout_docs.py` asserts
  specific wording in ADR-007, and
  `cuprum/unittests/test_line_observation_docs.py` asserts an explicit list of
  module paths in the developers' guide's line observation section, so an ADR
  or guide edit can fail `make test` while reading as prose. The maturin wheel
  snapshot in `cuprum/unittests/__snapshots__/test_maturin_build.ambr`
  additionally lists every shipped module, so a new `cuprum/*.py` file changes
  it. Impact: the split carries edits to ADR-007, `docs/cuprum-design.md`, and
  `docs/developers-guide.md`, and the wheel snapshot is regenerated.
- Observation (module split, third pass): a *logger name* can be a
  test-visible interface. `cuprum/unittests/test_subprocess_drain_logging.py`
  pins `_DRAIN_LOGGER = "cuprum._subprocess_wait"` and asserts on records from
  that logger, so moving the drain's `_LOGGER.debug` calls out of
  `_subprocess_wait` would break the test even though behaviour was identical —
  `logging.getLogger(__name__)` resolves to the *defining* module's name.
  Evidence: the constant and its `caplog.at_level(..., logger=_DRAIN_LOGGER)`
  use read before the split. Impact: the child-exit half of the split was
  chosen so the drain calls stay put: `_report_timeout_expiry`, which
  `_subprocess_deadline` calls, takes the observation rather than a logger and
  routes its own reporting through `cuprum._timeout_reporting`, and
  `_await_process_exit` is not monkeypatched anywhere. The new modules are
  therefore logger-name-neutral by construction, not by luck.
- Observation (module split, third pass): a *contravariant* parameter rejects
  the narrower annotation.
  `type _EofGraceWaiter = Callable[[_ConsumerPair], ...]` with
  `type _Consumer = Task[str | None] | None` was rejected at five test call
  sites whose helpers annotated their parameter as the non-optional
  `tuple[Task[str | None], Task[str | None]]` — ty reports "`_ConsumerPair` is
  not assignable to `tuple[Task[str | None], Task[str | None]]`", i.e. it is
  the *helper* that does not accept what the field may pass, not the field that
  is too wide. Impact: the four helpers now annotate the parameter as
  `_ConsumerPair` and drop the `None` slots before gathering, which is what the
  real `_await_eof_grace` does with the same value.
- Observation (verification): the `asyncio.gather` return-type diagnostic in
  `cuprum/_line_stream/drain.py` was *not* a missing-annotation problem to
  silence. `gather`'s variadic overload returns `list`, and only its
  fixed-arity overloads return `tuple`, so a returned gather result can never
  satisfy a declared `tuple[...]` return type. Impact: the two settled results
  are unpacked by index instead; an annotation-only fix at the call site would
  have left the mismatch for the next reader.

## Decision log

- Decision: fix the ignored-encoding defect by threading a codec value rather
  than by widening `_spawn_stdin_writer` with two more scalar parameters.
  Rationale: the spawn call sites are already at the repository's argument
  ceiling (`max-args = 5`), and the two settings are always read together from
  one source, so a single value keeps the signature legal and the call sites
  readable. Date/Author: 2026-09-27, implementation agent.
- Decision: model streaming input as a wrapper type, `StdinStream`, over an
  async iterable of `str | bytes`, rather than accepting a bare async iterable
  in `stdin=`. Rationale: a bare iterable makes "is this a payload or a
  producer?" ambiguous for `bytes` (which is itself iterable of ints), and the
  wrapper is where the encoding policy, the `aclose()` contract, and the
  cancellation semantics get documented. It also leaves room to add the payload
  form as a variant later without another signature change. Date/Author:
  2026-09-27, planning agent.
- Decision: model redirection as a tagged union `StdioTarget` with four
  variants — library-owned pipe, inherited, owned path, borrowed descriptor or
  file object — resolved at spawn time on both backends. Rationale: the
  borrowed-versus-owned distinction is the whole safety question of the
  feature, and only a type can carry it. Resolving at spawn time (not at
  `RunOutputOptions` construction) means a target describing a file that does
  not exist yet is still valid configuration. Date/Author: 2026-09-27, planning
  agent.
- Decision: replace `_ExecutionState.stdin_data: bytes | None` with a
  four-variant internal `_StdinPlan` and carry output targets beside it.
  Rationale: the run path already resolves everything before the spawn; a plan
  value keeps "resolved once, used by every backend" true, and avoids threading
  four public types down through five internal layers. Date/Author: 2026-09-27,
  planning agent.
- Decision: report early child-side pipe closure as an `ExecEvent` with phase
  `stdin_error` rather than raising. Rationale: a child that reads only part of
  its input and exits is normal, not exceptional (`head` is the canonical
  example). The information is worth observing, and the exit code is already
  the caller's signal. Date/Author: 2026-09-27, planning agent.
- Decision: keep `Pipeline` stage wiring out of scope.
  Rationale: the issue's requested scope is the application API for a single
  command's standard streams; pipeline stages already have a library-owned pipe
  from `Pipeline` and a stage-level redirection contract needs its own design
  for inter-stage interception. Date/Author: 2026-09-27, planning agent.
- Decision: split the streaming writer out along the payload/producer seam
  rather than along a codec/IO seam or by trimming docstrings. Rationale: the
  module's own docstring already separates the two sources by how much of the
  payload exists at once — complete in memory versus pulled a chunk at a time —
  so the split reinforces the existing boundary instead of introducing a new
  one. Trimming prose was not available: the docstrings carry the early-close
  policy, the incremental-encoder rationale, and the reason the codec cannot be
  read off the process, and each of the last two records a defect already paid
  for. The ADR-007 roster also constrained the choice: `_emit_stdin_error`,
  `_write_stdin`, and `_spawn_stdin_writer` are named there as
  `_subprocess_stdin`'s, so the dispatcher stayed put and only the producer
  path moved, which keeps the ADR's ownership claim true as written.
  Date/Author: 2026-09-27, implementation agent.
- Decision: import `_write_stdin_stream` inside `_spawn_stdin_writer`'s body
  rather than at module scope in both directions. Rationale: the streaming
  module imports the pipe primitives (`_close_stdin`, `_emit_stdin_error`) at
  module scope, so a module-scope import of the streaming writer in the
  dispatcher would close a load-time cycle in which neither module is complete
  when the other needs it. The module `__init__` order already depends on
  nothing in either module importing `cuprum.sh` at runtime, so the deferred
  import is the repository's established pattern for this shape of dependency —
  `_source_error` reaches `StdinSourceError` through the same lazy shim for the
  same reason. Date/Author: 2026-09-27, implementation agent.
- Decision: clear the third module-size pass by extracting
  `cuprum/_subprocess_spawn.py` (stdio mapping, owned-descriptor lifetime, the
  spawn call) and `cuprum/_subprocess_deadline.py` (the child-exit wait and its
  deadline) rather than by any of the alternatives — trimming docstrings,
  moving the drain, or suppressing the finding. Rationale: `too-many-lines` is
  in `pylint`'s `enable = [...]` list, so it cannot be annotated away, and the
  docstrings in both modules carry policy that no other artefact records (the
  early-close contract, the owned-versus-borrowed rule, the sampling order the
  timing tests pin). The seam for each module is the one its own docstring
  already drew: `_subprocess_execution`'s is "everything below the
  orchestration", and the spawn binding is exactly that; `_subprocess_wait`'s
  is the child-exit wait versus the consumer drain. The drain could not move —
  a test pins the logger name `cuprum._subprocess_wait`, and
  `logging.getLogger(__name__)` resolves to the defining module — so drawing
  the seam on the *other* side of the drain was the only split available that
  left the test-visible interface alone. Both moved names are re-exported from
  their original modules, so no import path or monkeypatch seam changes.
  Date/Author: 2026-09-27, implementation agent.
- Decision: annotate the four test helpers' `eof_grace_waiter` parameter as
  `_ConsumerPair` rather than widening `_EofGraceWaiter` back to the structural
  form. Rationale: the parameter is contravariant, so the *helper* is what must
  accept what the field may pass; the wider alias was rejected precisely
  because the tests' annotations were narrower than the value the field can
  hold. Naming the shared alias also keeps the two ends of the seam describing
  the same thing. Date/Author: 2026-09-27, implementation agent.

## Outcomes & retrospective

Not yet populated; this section is completed at each milestone boundary and
finalized when the plan reaches `COMPLETE`.

## Context and orientation

Cuprum is a typed wrapper over `asyncio` subprocesses: a caller allowlists
programs in a `ProgramCatalogue`, builds a `SafeCmd` with `cuprum.sh.make`, and
runs it with `SafeCmd.run` (async), `SafeCmd.run_sync` (blocking),
`SafeCmd.lines` (async line iteration), or composes several into a `Pipeline`.

The run path, in order, is:

1. `cuprum/sh/safe_cmd.py` — `SafeCmd.run` resolves the `ExecutionContext`,
   resolves `stdin` to bytes, and builds a `_ExecutionState`.
2. `cuprum/_command_internals.py` — `_build_subprocess_execution` turns the
   state into a `_SubprocessExecution`; `_run_prepared_command` opens the sink
   session and drives `_execute_with_hooks`.
3. `cuprum/_subprocess_execution.py` — `_SubprocessExecution` holds the run's
   frozen configuration. `_spawn_subprocess` now lives in
   `cuprum/_subprocess_spawn.py`, which maps the resolved stdio onto the value
   `Popen` receives, choosing `PIPE` or `DEVNULL` per stream from the
   `consumes_stdout`/`consumes_stderr` properties, opens and closes each
   cuprum-owned target, and is re-exported here so the composition root keeps
   its one import path. Two execution paths exist:
   `_run_subprocess_without_streams` (direct) and, for line observation and the
   pipeline, the streamed path.
4. `cuprum/_wait4_process.py` — on POSIX with `os.wait4` available,
   `_Wait4Process` owns the child and reaps it with resource measurement;
   `spawn_direct_process` takes a `DirectProcessConfig` whose `stdin`,
   `stdout`, and `stderr` are today `int | None`, and `connect_pipes()` wraps
   each non-`None` pipe in `StreamReader`/`StreamWriter`. When `wait4` is
   unavailable, plain `asyncio.create_subprocess_exec` is used instead.
5. `cuprum/_subprocess_stdin.py` — `_spawn_stdin_writer` starts a task that
   writes the already-resolved bytes and closes the pipe. A `StdinStream`
   producer is started from the same dispatcher but written by
   `cuprum/_subprocess_stdin_stream.py`, which pulls it one chunk at a time.
6. `cuprum/_subprocess_wait.py` — `_reconcile_run_tasks` cancels the stdin
   writer, drains the stream consumers, and settles diagnostics on every exit
   path. `_RunTaskOwnership` is the bundle it takes. The child-exit half of the
   wait — `_wait_for_exit_code` and `_wait_for_exit_code_within_timeout` —
   moved to `cuprum/_subprocess_deadline.py` and is re-exported here.
7. `cuprum/_subprocess_stream_run.py` and `cuprum/_line_stream/` — the streamed
   path and the `lines()` path, both of which build consumers and a stdin
   writer the same way.

Terms used in this plan:

- **Borrowed** means cuprum uses a resource it does not own and must not close.
- **Owned** means cuprum opened the resource for this run and must close it.
- **Pinned** means a stream bound to something other than a library-created
  pipe — inherited, `/dev/null`, a file, or a borrowed descriptor. A pinned
  stream has no parent-side pipe and therefore nothing for cuprum to consume or
  write.
- **Pull-after-drain** means the producer is advanced only after the previous
  chunk has been fully drained into the child, which is what bounds memory.
- **Early close** means the child closed its stdin before the producer was
  exhausted, which on POSIX surfaces as `BrokenPipeError`/`EPIPE`.

Public surface today lives in `cuprum/sh/execution.py` (`ExecutionContext`,
`StdinInput`, `TimeoutExpired`), `cuprum/sh/output.py` (`RunOutputOptions`,
`IOOptions`), and `cuprum/sh/results.py`; `cuprum/sh/__init__.py` and
`cuprum/__init__.py` re-export them explicitly and maintain a sorted `__all__`.

## Conformance basis

The governing upstream artefacts are the issue text itself and the repository's
own architecture records; there is no separate Terms of Reference or
technical-design revision for this change, and this plan does not invent one.

```plaintext
issue#445-REQ-streaming-stdin   -> EP-M1, EP-M2, EP-M3 -> tests::stdin_stream
issue#445-REQ-file-redirection  -> EP-M1, EP-M2       -> tests::redirect
issue#445-REQ-lifetimes         -> EP-M2, EP-M4       -> tests::redirect_ownership
issue#445-REQ-limits            -> EP-M1, EP-M4       -> docs/users-guide.md
issue#445-ACC-backpressure      -> EP-M3              -> tests::stdin_stream_bounded
issue#445-ACC-teardown          -> EP-M3              -> tests::stdin_stream_failure
ADR-007 (module boundaries)     -> EP-M2              -> code review + module list
ADR-009 (Oxford spelling)       -> EP-M4              -> make spelling
```

ADR-007 keeps subprocess plumbing split across the single-responsibility
modules listed in `Context and orientation`; this plan adds no new module
boundary and moves no existing one, so the ADR is satisfied by construction and
re-checked at each milestone boundary. The 400-line module ceiling is the
practical expression of that ADR here: `_subprocess_execution.py` (364 lines),
`_subprocess_wait.py` (393), and `sh/safe_cmd.py` (399) are all near the cap,
so new logic goes into new small modules rather than into them.

## Verification plan

Each invariant names the artefact that discharges it, the command that runs it,
and why a passing result cannot be vacuous.

- Obligation: `INV-1 — bounded memory`. A run whose producer yields more bytes
  than the configured buffering bound completes without the parent retaining
  more than one chunk at a time, while the child consumes slowly. Method:
  integration test with a slow-reading child (a Python child that reads a fixed
  number of bytes per iteration after a short sleep) and a producer that
  records how many chunks it has yielded at each moment; plus a peak-RSS check
  via the run's own resource measurement on the `wait4` path. Rationale: the
  property is about retained state over time, which no single assertion
  captures; the chunk-yield counter is the observable proxy. Domain: payloads
  of 1 chunk, exactly the bound, bound + 1, and 64 MiB across 4 KiB chunks;
  reader delays of 0 s and 50 ms. Artefact:
  `cuprum/unittests/test_safe_cmd_stdin_stream.py`. Evidence: `make test`; the
  test fails before EP-M3 with the producer fully consumed before the child
  read anything, and passes after. Non-vacuity: a deliberately eager
  implementation (collect the iterable into a list first) must fail the counter
  assertion; the test module includes that variant as a negative control so the
  assertion is shown to bite.

- Obligation: `INV-2 — byte-exact delivery`. For a producer yielding a
  generated list of `bytes` and `str` chunks, the child receives exactly the
  concatenation of the encoded chunks, with no reordering, no duplication, and
  no truncation at chunk boundaries. Method: property test over generated chunk
  lists (sizes spanning 0, 1, the pipe buffer size, and several buffer sizes;
  mixed `str`/`bytes`; empty chunks). Rationale: chunk-boundary handling is
  where encoding and pipe-write bugs live, and the input space is cheap and
  broad. Domain: `hypothesis` strategies over lists of `bytes` and `str`.
  Artefact: `cuprum/unittests/test_stdin_property_based.py` (extended).
  Evidence: `make test`; a mutant that drops the final encoder flush must
  produce a counterexample with a multi-byte tail. Non-vacuity: the generator
  must reach the empty-list and single-empty-chunk cases, which are exactly
  where an off-by-one flush fails; assert the generator's classification counts
  stay above zero for those classes.

- Obligation: `INV-3 — producer failure propagates`. When the producer raises,
  the child is terminated, a `StdinSourceError` reaches the caller, and no
  writer task, pipe, or child process outlives the run. Method: parameterized
  integration tests over the strategy fixture, with a producer that raises
  after N chunks for N in {0, 1, mid, last}. Rationale: the failure must be
  raised on every exit path, and the exit paths are a finite enumeration.
  Domain: `run()` and `run_sync()`; capture on and off; raise before the first
  chunk, between chunks, and after the final chunk. Artefact:
  `cuprum/unittests/test_safe_cmd_stdin_stream.py`. Evidence: `make test`;
  before EP-M3 the exception surfaces as a bare producer exception or is
  swallowed, after it as `StdinSourceError` with the child's exit observed.
  Non-vacuity: the after-final-chunk case is the negative control — it must
  *not* raise, since the producer completed successfully; an implementation
  that wraps every exception indiscriminately fails that case.

- Obligation: `INV-4 — cancellation and timeout leave no writer`. A cancelled
  run and a timed-out run each leave no pending stdin writer and no live child.
  Method: parameterized tests that cancel the run task or set a short timeout
  while the child is still reading, then assert on `asyncio.all_tasks()`
  membership and on the child's exit. Rationale: exactly two termination paths
  exist and both must be enumerated. Domain: `timeout=` of 0.05 s and of `-1`;
  cancellation delivered while the producer is mid-chunk and while it is idle.
  Artefact: `cuprum/unittests/test_safe_cmd_stdin_stream.py`. Evidence:
  `make test`; pre-EP-M3 phantom writer tasks remain in `all_tasks()` after the
  run returns. Non-vacuity: the mid-chunk and idle cases exercise different
  suspension points; a negative control asserts that a *completed* run also
  leaves no pending tasks, so the sweep itself is shown to be satisfiable.

- Obligation: `INV-5 — ownership after every outcome`. An owned path target's
  descriptor is closed exactly once, after spawn, and a borrowed descriptor or
  file target is never closed, on normal exit, non-zero exit, timeout, and
  spawn failure. Method: parameterized tests that hand cuprum an `os.pipe()`
  read end as a borrowed target and assert the caller can still use its own end
  afterwards; a counting wrapper around `os.close` (or a `fileno`-tracking
  helper) asserts the owned path's descriptor count. Rationale: the
  borrowed/owned distinction is the safety-critical part of the feature and is
  a finite table. Domain: {owned path, borrowed fd, borrowed file} × {exit 0,
  exit non-zero, timeout, spawn failure}. Artefact:
  `cuprum/unittests/test_safe_cmd_redirect.py`. Evidence: `make test`; a mutant
  that closes a borrowed descriptor makes the caller's subsequent write fail
  with `EBADF`. Non-vacuity: the borrowed cases are the control for the owned
  case — an implementation that closes everything passes the owned assertions
  and fails these; POSIX-only descriptor checks are skipped on Windows with an
  explicit marker rather than silently omitted.

- Obligation: `INV-6 — piped-only consumers and writers`. A stream bound to a
  file, inherited, or `/dev/null` has no parent-side `StreamReader` and no
  stdin writer; a stream left as a pipe still has both. Method: parameterized
  unit tests over the four `StdioTarget` variants times the three streams,
  asserting on the values cuprum passes to the spawn layer and on which
  consumer tasks exist. Rationale: this is the invariant whose violation
  produces hangs rather than failures, so it needs a direct structural
  assertion, not an end-to-end one. Domain: each stream independently
  redirected while the others are captured; all three redirected at once.
  Artefact: `cuprum/unittests/test_safe_cmd_redirect.py`. Evidence:
  `make test`; skipping the `connect_pipes()` gate causes a hang that the
  test's own timeout converts into a failure. Non-vacuity: the "other two
  streams still captured" half of each case proves the assertion can
  distinguish a fully-pinned run from a partially-pinned one.

- Obligation: `INV-7 — existing behaviour unchanged`. With no new arguments,
  every existing test in the suite passes unmodified. Method: the existing
  suite. Rationale: the whole existing suite is the regression oracle; no
  narrower test would be adequate. Domain: all of `cuprum/unittests`, `tests`,
  and the Rust crates. Artefact: the repository's own tests. Evidence:
  `make test`; any modification to an existing expected output is a failure to
  investigate, not to bless. Non-vacuity: snapshot tests (`syrupy`) and
  exact-string event assertions already fail on single-byte changes, so their
  passing is informative.

- Obligation: `INV-8 — rejection of contradictory combinations`. Combinations
  that cannot be honoured are rejected at construction with a message naming
  the offending fields. Method: parameterized tests over the rejected
  combinations. Rationale: a finite, enumerable set of rules belongs in a
  table. Domain: at least — a target for a stream that capture/echo requires to
  be a pipe; the same path supplied for both stdout and stderr; a target that
  is neither a path, an int, nor a file object; `lines()` with a redirected
  stdout. Artefact: `cuprum/unittests/test_safe_cmd_redirect.py`. Evidence:
  `make test`; each row must raise, and a `.txt`-style message assertion pins
  the field names. Non-vacuity: each rejected row is paired with an accepted
  near-miss (for example, distinct paths for stdout and stderr) so the rule is
  shown to be a boundary rather than a blanket refusal.

Axiom (external, not to be verified internally): `subprocess.Popen` assigns
`self.stdin`/`self.stdout`/`self.stderr` only for `asyncio.subprocess.PIPE`,
and passes raw descriptors and file objects to the child without retaining
them. This was read from CPython's source (`subprocess.py`,
`asyncio/unix_events.py`) and is exercised, not assumed, by INV-5 and INV-6,
which would fail if the axiom were false. Axiom: `os.wait4` resource
measurement is available on Linux, making the `_Wait4Process` backend the
default on the development host; the fallback `create_subprocess_exec` path is
covered by the same parameterized tests, selected by monkeypatching
`wait4_resource_measurement_available()` to return `False`.

No formal proof, model check, or bounded model check is planned. The change
introduces no arithmetic invariant, no concurrency protocol with an unbounded
state space, and no `unsafe` boundary; its obligations are finite enumerations
over streams, targets, outcomes, and exit paths, plus one broad-input property.
The one genuinely temporal obligation, INV-1, is discharged by a counter-based
witness rather than by a proof, and that residual gap is recorded here rather
than papered over: the test shows bounded retention for the exercised payload
sizes, not for all conceivable schedules.

## Plan of work

The work is four milestones, one per task of the issue's coding plan. Each is a
coherent plateau: the repository builds, all gates pass, and the feature is
usable at whatever surface the milestone completed. No compatibility shim is
introduced at any boundary — the widest signature change (`stdin` widening) is
made in one step at EP-M1, and `_StdinPlan` replaces `stdin_data` outright at
EP-M2 with every caller updated in the same commit.

Stage A (understanding) is complete: it is the reconnaissance recorded in
`Progress` and `Surprises & discoveries`. Stage B, C, and D are the Red, Green,
and Refactor steps recorded per milestone below.

### EP-M1 — public vocabulary and options

Red: add `cuprum/unittests/test_public_api.py` assertions that `StdinStream`,
`StdinSourceError`, and `StdioTarget` are importable from `cuprum` and
`cuprum.sh`, that `SafeCmd.run`'s `stdin` annotation resolves to the widened
union, and that `RunOutputOptions` rejects a path target for a stream that
capture requires to be a pipe. Run `make test` and expect `ImportError`.

Green:

- `cuprum/sh/execution.py`: add `StdinStream` (frozen slot dataclass wrapping a
  `cabc.AsyncIterable[str | bytes] | cabc.AsyncIterator[str | bytes]`, with a
  documented `aclose()` contract and a `chunks(ctx)` async generator that
  encodes `str` incrementally with `ctx.encoding`/`ctx.errors`); add
  `StdinSourceError(Exception)`; add
  `type StdinSource = StdinInput | StdinStream`; extend `__all__`.
- `cuprum/sh/output.py`: add
  `type StdioTarget = StdioPipe | StdioInherit | StdioPath | StdioFd` (or an
  equivalent tagged union of frozen dataclasses) and the `stdout`/`stderr`
  fields on `RunOutputOptions`; extend `__post_init__` with the rejection
  rules; document each variant's ownership.
- `cuprum/sh/safe_cmd.py`: widen `stdin` on `run`, `run_sync`, and `lines` to
  `StdinSource | None = None`; keep resolving `StdinInput` to bytes exactly as
  now and pass a `StdinStream` through unresolved.
- `cuprum/sh/__init__.py`, `cuprum/__init__.py`: export `StdinStream`,
  `StdinSourceError`, `StdioTarget`, `StdinSource`; keep `__all__` sorted.
- `tests/helpers/execution.py`: widen `_RunKwargs.stdin` to `StdinSource`.

Validation: `make test` focused on the new public-API tests; `make typecheck`.

### EP-M2 — resolved planning and spawn-time binding

Red: add tests that redirect the child's stdout to a temporary path and assert
the file's contents and that its descriptor is closed after the run; add the
structural INV-6 assertions. Expect failure or a hang that the test's own
timeout converts into a failure.

Green:

- New module `cuprum/_stdio_plan.py` (kept small, per ADR-007): the four-variant
  `_StdinPlan` (`_NoStdin`, `_PayloadStdin`, `_StreamStdin`, `_PipeStdin`), the
  resolved `_StdioBinding` for each output stream, `_resolve_stdin_plan`, and
  `_resolve_stdio_target` (which flushes a borrowed file object and returns the
  descriptor to pass).
- `cuprum/_command_internals.py`: `_ExecutionState.stdin_data` becomes
  `stdin_plan: _StdinPlan`, plus `stdout_target`/`stderr_target`; thread both
  through `_build_subprocess_execution`.
- `cuprum/_subprocess_execution.py`: `_SubprocessExecution` carries the plan and
  targets; `_spawn_subprocess` maps them to stdio values, opens owned paths
  immediately before the spawn call, and closes only the owned descriptors in a
  `finally` right after; `consumes_stdout`/`consumes_stderr` gain "and the
  stream is still a pipe".
- `cuprum/_wait4_process.py`: `DirectProcessConfig` gains the pipe-ness
  information (a `pipes: frozenset[str]` field whose default preserves today's
  behaviour); `_Wait4Process.connect_pipes()` attaches a `StreamReader` only
  for streams named in it; the `create_subprocess_exec` fallback receives the
  same values.
- `cuprum/_subprocess_streams.py`, `cuprum/_subprocess_stream_run.py`,
  `cuprum/_line_stream/spawn.py`, `cuprum/_subprocess_execution.py`: build a
  consumer only for a piped stream, tolerating `None` in the consumer tuple
  everywhere the drain machinery indexes it; build the stdin writer only for a
  pipe plan.

Validation: `make test`; the red tests pass; no existing test changes.

### EP-M3 — bounded streaming and producer-failure teardown

Red: add the INV-1 counter test, the INV-3 failure tests, and the INV-4
cancellation/timeout tests. Expect the counter assertion and the
`StdinSourceError` assertion to fail.

Green:

- `cuprum/_subprocess_stdin_stream.py` (created by the split recorded in
  `Progress`; this list named `_subprocess_stdin.py` when EP-M3 was planned):
  obtain the iterator with `aiter()`, encode `str` chunks incrementally through
  an `IncrementalEncoder`, write a chunk and `await drain()` before pulling the
  next, flush the encoder at completion, emit one `stdin` event per chunk with
  its `byte_count`, treat `BrokenPipeError`/`EPIPE` as an early close and emit
  exactly one `stdin_error` observation, wrap producer and encoding exceptions
  in `StdinSourceError`, propagate `CancelledError` unchanged, and on every
  exit path call the iterator's `aclose()` when present, then close the pipe
  and await `wait_closed()`.
- `cuprum/_subprocess_wait.py`: watch process exit and the writer together; on
  an early `StdinSourceError`, cancel the exit wait, escalate termination,
  reconcile consumers, and raise; raise on a late producer failure too, after
  reconciling.
- `cuprum/_subprocess_stream_run.py`, `cuprum/_line_stream/spawn.py`: route
  through the same writer; finalize the source on `aclose()` and on
  `async with` exit.

Validation: `make test`; INV-1, INV-3, INV-4 pass; existing timeout and
cancellation tests pass unmodified.

### EP-M4 — verification suites and documentation

Red: none — this milestone is the completion of the verification artefacts and
the documentation.

Green:

- `cuprum/unittests/test_safe_cmd_stdin_stream.py`: the streaming lifecycle
  suite, parameterized over the `execution_strategy` fixture and capture on/off.
- `cuprum/unittests/test_safe_cmd_redirect.py`: the redirection and ownership
  suite, including every rejection rule and the POSIX-only skip.
- `cuprum/unittests/test_stdin_property_based.py`: generated chunk lists.
- `docs/users-guide.md`: the streaming and redirection contract, with the
  ownership table, the shared-offset guidance for borrowed descriptors, and the
  unsupported combinations and platform limits.
- `docs/cuprum-design.md`: the resolved-plan and teardown design, and the
  updated public signatures.
- `docs/roadmap.md`: a new 0.2.0 entry for `#445` beside the direct-stdin
  section, marked complete.

Validation: `make test`, `make check-fmt`, `make lint`, `make typecheck`,
`make markdownlint`, `make spelling`, `make nixie` — run sequentially through
`scrutineer`, each captured under `/tmp`.

## Milestones and plateaus

- Identifier and outcome: `EP-M1` — the new public names exist, are exported,
  and are validated at construction; the run path still behaves exactly as
  before because nothing consumes them yet.
- Requirements and gaps: discharges `issue#445-REQ-streaming-stdin` (surface
  half) and `issue#445-REQ-limits` (declared rejections).
- Acceptance evidence: `test_public_api.py` passes; `RunOutputOptions`
  rejection rows raise; `make typecheck` passes.
- Conformance check: no interior module imports `cuprum.sh` at runtime; no new
  module exceeds 400 lines; `RunOutputOptions` remains constructible with its
  existing field names and by keyword.
- Recovery: `git revert` the milestone commit; nothing depends on it.
- Remaining gaps: nothing consumes the new types; `stdin=StdinStream(...)`
  raises a not-yet-implemented error until EP-M2/EP-M3.
- Compatibility decision: none. `RunOutputOptions` is pre-1.0 and the addition
  is purely additive, so no compatibility layer is required or added.

- Identifier and outcome: `EP-M2` — redirection works end to end on both
  backends, descriptors are opened and closed at the right moments, and
  non-piped streams have no consumer or writer.
- Requirements and gaps: discharges `issue#445-REQ-file-redirection` and
  `issue#445-REQ-lifetimes`.
- Acceptance evidence: `test_safe_cmd_redirect.py` passes on both the
  `_Wait4Process` and `create_subprocess_exec` paths.
- Conformance check: ADR-007 module boundaries unchanged; the pipe/descriptor
  separation is expressed in the spawn layer, not leaked upward.
- Recovery: `git revert` the milestone commit; EP-M1's types become inert
  configuration again.
- Remaining gaps: streaming stdin still resolves eagerly, so `StdinStream` is
  accepted but not yet pull-based.
- Compatibility decision: none.

- Identifier and outcome: `EP-M3` — streaming stdin is bounded, failure-safe,
  cancellable, and timeout-safe on all three execution paths.
- Requirements and gaps: discharges `issue#445-ACC-backpressure`,
  `issue#445-ACC-teardown`, and the behavioural half of
  `issue#445-REQ-streaming-stdin`.
- Acceptance evidence: INV-1, INV-3, and INV-4 tests pass; the existing
  timeout and cancellation suites pass unmodified.
- Conformance check: the writer owns the iterator and the pipe and finalizes
  both on every path; no new observation vocabulary; `_shielded_cleanup` still
  guards every cancellation-sensitive await.
- Recovery: `git revert` the milestone commit; redirection from EP-M2 is
  unaffected.
- Remaining gaps: documentation and the consolidated suites.
- Compatibility decision: none.

- Identifier and outcome: `EP-M4` — the feature is documented and its
  obligations are discharged by named artefacts.
- Requirements and gaps: discharges every remaining `issue#445` item and
  `ADR-009` conformance.
- Acceptance evidence: the full gate list passes through `scrutineer`.
- Conformance check: every trace link in `Conformance basis` resolves to a
  passing artefact; the roadmap's direct-stdin section is extended rather than
  rewritten.
- Recovery: documentation-only revert is safe.
- Remaining gaps: none within the issue's scope.
- Compatibility decision: none.

## Concrete steps

Run everything from the worktree root. Long output goes through `tee` so the
tail is reviewable.

1. Author and commit this plan:

   ```bash
   git add docs/execplans/issue-445-support-streaming-stdin-and-explicit-file-redirection.md
   git commit -m "Add the execution plan for issue 445"
   ```

2. Per milestone, write the red tests, then run the focused suite:

   ```bash
   make test 2>&1 | tee /tmp/test-$(get-project)-$(git branch --show-current).out
   ```

3. Delegate the full commit-gate run to `scrutineer` and read the captured
   logs rather than re-running gates:

   ```plaintext
   ask scrutineer: run make check-fmt, make lint, make typecheck, make test,
   make markdownlint, make spelling, make nixie sequentially from the worktree
   root, capture each to /tmp/<gate>-<branch>.out, and report failures with the
   cited log path.
   ```

4. Then, and only then, request the CodeRabbit pass:

   ```bash
   coderabbit review --agent
   ```

   If the rate limit is hit, sleep with `vsleep $(shuf -i 45-90 -n 1)` minutes
   and retry.

5. Commit each milestone separately so the history is reviewable:

   ```bash
   git add -A
   git commit -m "Add public streaming stdin and stdio target types"
   ```

## Validation and acceptance

Red-Green-Refactor evidence is recorded per milestone in `Plan of work`. The
focused command throughout is `make test`, and the acceptance criterion for
each red step is that the named test fails for the stated reason — an
`ImportError` for EP-M1, a contents or descriptor assertion for EP-M2, a
chunk-counter or exception-type assertion for EP-M3.

Acceptance as behaviour:

- Streaming a payload larger than the buffering bound while the child consumes
  slowly completes, and the producer's yield counter shows at most one
  outstanding chunk.
- Early pipe closure, producer failure, cancellation, and timeout each complete
  with no leaked writer task and no live child.
- A file target receives the child's exact bytes, and its descriptor is closed
  after the child was spawned — verified on normal exit, non-zero exit,
  timeout, and spawn failure.
- A borrowed descriptor and a borrowed file object are usable by the caller
  afterwards.
- Capture, echo, idle observation, and `on_line` continue to work in
  combination with redirection, and the unsupported combinations are rejected
  with a message naming the fields.

Quality criteria: `make test` passes; `make typecheck` passes; `make check-fmt`,
`make lint`, `make markdownlint`, `make spelling`, and `make nixie` pass; no
existing expected output is modified; no new runtime dependency appears in
`pyproject.toml`.

Quality method: the sequential gate run performed by `scrutineer` and reported
with per-gate logs, followed by `coderabbit review --agent` with all concerns
cleared before the next milestone.

## Idempotence and recovery

Every step is a source edit followed by tests, so re-running is safe:
`make test` and the other gates are read-only with respect to tracked files,
with two exceptions to watch. `make lint` regenerates `typos.toml`; commit the
refreshed file as its own commit rather than reverting it, or the gate dirties
the tree again. `make fmt` rewrites Markdown through `mdtablefix`, which
reflows prose narrower than 80 columns; if it changes a handwritten paragraph,
take the tool's output as the authority. Temporary files go under `/tmp` only,
never inside the repository; probe files placed in the repository root break
`check-fmt` and must be excluded via `.git/info/exclude` rather than deleted
and regenerated.

Rollback is per-milestone `git revert`. Because each milestone is a coherent
plateau, reverting the latest one is always safe.

## Artefacts and notes

The load-bearing external fact, captured for the reader who has not read
CPython's source:

```plaintext
Popen._get_handles: PIPE -> os.pipe(); DEVNULL -> _get_devnull();
raw int -> used directly as the child-end fd; otherwise <obj>.fileno().
self.stdin/self.stdout/self.stderr are assigned only when the corresponding
parent pipe end is != -1, i.e. only for PIPE. Raw ints and file objects leave
them None and are never closed by the parent.
```

The early-close observation shape:

```python
# one event per written chunk, then at most one early-close event
ExecEvent(phase="stdin", details=_EventDetails(byte_count=4096, ...))
ExecEvent(phase="stdin_error", details=_EventDetails(error_type="BrokenPipeError", ...))
```

## Interfaces and dependencies

No new external dependency. The following public names must exist at the end of
EP-M1, in `cuprum/sh/execution.py` and `cuprum/sh/stdio.py` respectively (the
latter having been extracted from `cuprum/sh/output.py` at the 04:45Z
relocation, which re-exports it), and be re-exported from `cuprum.sh` and
`cuprum`:

```python
@dc.dataclass(frozen=True, slots=True)
class StdinStream:
    """A library-owned, bounded, pull-after-drain producer for a child's stdin."""

    chunks: cabc.AsyncIterable[str | bytes] | cabc.AsyncIterator[str | bytes]


class StdinSourceError(Exception):
    """Raised when a streaming stdin producer or its encoder fails."""


type StdinSource = StdinInput | StdinStream


@dc.dataclass(frozen=True, slots=True)
class StdioTarget:
    """Where a child's standard stream is bound; four tagged variants."""

    kind: _StdioKind = "pipe"  # "pipe" | "inherit" | "path" | "fd"
    value: Path | int | typ.IO[bytes] | typ.IO[str] | None = None

    @staticmethod
    def pipe() -> StdioTarget: ...  # cuprum-owned pipe

    @staticmethod
    def inherit() -> StdioTarget: ...  # parent's stream, untouched

    @staticmethod
    def path(path: Path) -> StdioTarget: ...  # cuprum opens and closes

    @staticmethod
    def fd(fd: int | typ.IO[bytes] | typ.IO[str]) -> StdioTarget: ...  # borrowed
```

`StdioTarget` is a single tagged frozen dataclass rather than a union of four
types, because callers construct variants through the staticmethods above and a
union alias cannot host methods. The free staticmethods are the whole point:
`StdioTarget.pipe()` reads as one name at the call site, and `is_owned_path`
tells EP-M2 which descriptors cuprum must close.

`SafeCmd.run`, `SafeCmd.run_sync`, and `SafeCmd.lines` gain the widened
annotation `stdin: StdinSource | None = None`; `RunOutputOptions` gains
`stdout: StdioTarget | None = None` and `stderr: StdioTarget | None = None` as
keyword-only fields after the existing ones, and inherits into `IOOptions`
unchanged. Internally, `cuprum/_stdio_plan.py` must define `_StdinPlan` with
its four variants and the two resolvers named in EP-M2.
