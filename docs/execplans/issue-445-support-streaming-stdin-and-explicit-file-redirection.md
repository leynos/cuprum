# Support streaming stdin and explicit file redirection (`#445`)

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: COMPLETE (one recorded deviation — the scope tolerance in `Tolerances`
was breached; see the entry in `Decision log` and `Outcomes & retrospective`)

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
import asyncio
import collections.abc
import pathlib
import sys

from cuprum import (
    ExecutionContext,
    Program,
    ProgramCatalogue,
    RunOutputOptions,
    StdioTarget,
    StdinStream,
    sh,
)

catalogue = ProgramCatalogue.from_programs(sys.executable, name="streaming")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "import sys; sys.stdout.write(sys.stdin.read().upper())")


async def _chunks() -> collections.abc.AsyncIterator[bytes]:
    """Yield the payload in bounded pieces rather than as one buffer."""
    for word in (b"alpha ", b"beta ", b"gamma"):
        yield word


async def main() -> None:
    """Feed the producer to the child and print the result's exit code."""
    result = await command.run(
        stdin=StdinStream(chunks=_chunks()),
        # capture must be off: the child writes straight to the file, so there
        # is no parent-side pipe for cuprum to read.
        output=RunOutputOptions(
            capture=False, stdout=StdioTarget.path(pathlib.Path("out.log"))
        ),
        context=ExecutionContext(),
    )
    print(result.exit_code)


asyncio.run(main())
```

and mean two new things. First, the producer's chunks are pulled one at a time,
written to the child's stdin, and drained before the next chunk is pulled, so
the writer cannot outrun the child and the payload is never held whole. That is
a bound on how far *ahead* the producer is pulled, not on the size of the chunk
being written: until `drain()` returns, the chunk just pulled and its encoded
payload are still in memory alongside the transport's write buffer and the OS
pipe, so peak memory is roughly the largest chunk yielded plus those buffers —
which is why a caller who cares about it should yield bounded-size chunks.
Second, the child's stdout is bound to a file cuprum opened for the run and
closed as soon as the child was spawned — the child keeps the descriptor for
its lifetime, cuprum keeps ownership of closing it, and the caller never sees a
leak.

The existing `stdin=StdinInput(...)` payload API and the inherited-stdin
default (the current `stdin=None`) behave exactly as they do today; this is an
additive widening, not a replacement.

Success is observable three ways. A child that reads 64 MiB slowly while our
producer yields 4 KiB chunks completes without the producer ever getting far
ahead of it: what the test measures is that lookahead — the pulls the writer
has taken ahead of the child's reads — and not cuprum's peak resident set,
which nothing measures. A run whose stdout is redirected to a file leaves that
file with the child's exact bytes and the file descriptor closed, both on
`exit_code == 0` and on a non-zero exit. And a producer that raises mid-stream
terminates the child, surfaces a `StdinSourceError`, and leaves no writer task,
no pipe, and no orphan process behind.

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

- [x] (2026-09-27 22:05Z) Re-gate after the round-4 repair. The first attempt
  aborted at `python-lint`: the new regression test's `_collect_stdout_lines`
  helper used `# noqa: ANN401`, which this repository no longer honours
  (`ruff: ignore[...]` is the accepted spelling), and the helper's manual
  append loop tripped the manual-list-comprehension rule. Rather than suppress
  the annotation, the parameter is now typed `LineStream` — a public, exported
  type — so no annotation suppression is needed at all. The comprehension
  rewrite keeps the helper's non-vacuity: with the flush call removed, the test
  still fails, now with the child reading `[]` instead of the flushed
  `['caller-first', 'caller-second']`. The sweep was halted at the first
  failure and re-run on the repaired tree from gate 1, because gates after an
  abort prove nothing about the tree that follows.
- [x] (2026-09-27 21:30Z) Round-4 review actioned. One finding was a real
  defect: the borrowed-file flush sat in the resolver, so on the `lines()` path
  bytes the caller wrote between calling `lines()` and iterating it never
  reached the child. Fixed by carrying the borrowed object on `_StdioBinding`
  and flushing in `_subprocess_spawn._flush_borrowed_stdio` at the fork, with a
  regression test that fails on the old placement. Four further findings were
  accepted (users-guide default-output description, the execplan's
  non-executable example, the EP-M2 `connect_pipes()` description, and the
  stale flush location in the ADR and plan), and two were declined (the roadmap
  field rename, whose premise is false, and the `_StdioBinding` "data only"
  framing, which would put the flush and the fork back out of step). Also
  extracted two helpers from
  `test_unread_pipe_stays_devnull_on_the_fallback_backend` to clear CodeScene's
  Large Method advisory (93 → 28 non-blank LOC), with the red-green property
  re-verified after the extraction.
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
- [x] (2026-09-27 12:05Z) EP-M4 docs, committed as `28d86c56` (six files, 223
  insertions): the users' guide's streaming contract and redirection ownership
  vocabulary, the executed `redirect-stdout-to-a-file` example, the
  `cuprum-design.md` roster entry, the append-only ADR-007 addendum for the
  rendezvous, `roadmap.md`, and `CHANGELOG.md`. `check-fmt` passed on this
  revision before the sweep was interrupted.
- [x] (2026-09-27 12:20Z) EP-M4 INV-1, the last verification artefact the
  traceability matrix names and the last one built. The plan's own text
  (`INV-1 — bounded memory`) asks for "a producer that records how many chunks
  it has yielded at each moment" plus an eager negative control; the
  reconciliation in `/tmp/em4-recon.md` had listed both as still missing. The
  pair landed in `cuprum/unittests/test_safe_cmd_stdin_stream.py`, which the
  module docstring already names as the artefact. The pacing child reads one
  byte, publishes a marker file, then reads the rest, so each pull can be dated
  against "the child has consumed something"; the same producer and the same
  child are then run behind `_drained_first`, which collects the iterable into
  a list before replaying it — the eager shape a resolve-up-front
  implementation would have. Measured directly, not asserted on faith:
  streaming 33 pulls of 256 before the child read, eager 256 of 256, against a
  cap of 64 (a quarter of the payload, and about four times the host's 64 KiB
  pipe capacity). Both margins are at least 2x, so neither side is near its
  threshold. The bound is deliberately loose rather than an exact count,
  because the pipe capacity is a property of the host and not of cuprum. Local:
  module 20 passed, the three EP-M4 modules together 49 passed, `ruff check` and
  `ruff format --check` clean, interrogate 100.0%.
- [x] (2026-09-27 12:00Z) Pushed `c100e810` and opened draft PR
  [#511](https://github.com/leynos/cuprum/pull/511) — `(#445)` in the title,
  `Closes #445` in the summary, the Lody session link under `## References`.
  The branch's changes touch no Rust file and no ADR-011: an earlier two-dot
  diffstat appeared to delete from both, but `origin/main` had advanced one
  commit (`7f762870`, Miri coverage) on the fetch, so those were main's own new
  work seen backwards. Confirmed with
  `git diff --name-only <merge-base>..HEAD -- rust/` (0 files) and a clean
  `git merge-tree --write-tree origin/main HEAD`.
- [x] (2026-09-27 11:59Z) Full seven-gate sweep on `c100e810`, all exit 0 in
  5 minutes (warm shared Cargo cache): `check-fmt` 5s, `lint` 55s (every
  sub-check ran to completion — ruff, interrogate 100%, pylint 10.00/10,
  df12-pylint, ambrleaks, skylos, rustdoc, clippy, whitaker, typos, yamllint,
  actionlint; actionlint did **not** stall this run), `typecheck` 5s,
  `markdownlint` 10s, `nixie` 5s, `spelling` 10s, `test` 205s (9 pytest blocks,
  largest 2543 passed / 63 skipped; nextest 125/125). All four new INV-1 cases
  ran and passed under both execution strategies. Logs under
  `/tmp/$ACTION-cuprum-<branch>.out`; summary
  `/tmp/gates-445/all-gates.summary`.
- [x] (2026-09-27 13:35Z) The `aca77867` sweep, and the two defects it found.
  Six of seven gates green (`check-fmt`, `typecheck`, `markdownlint`, `nixie`,
  `spelling`, `test`); `lint` exit 2 in `python-lint` on Skylos `SKY-U001` ×3 in
  `cuprum/sh/stdio.py`. That abort meant `rust-lint` and `github-actions-lint`
  were **never observed** — a "lint failed" verdict is not a full-lint verdict
  when the target is a serial chain that stops at its first failure. Both
  halves of the fix are recorded as Surprises above: the Skylos entry-point
  rule (naming the three callees, plus the matching
  `_RUNTIME_FUNCTION_ENTRY_POINTS` contract row that
  `test_skylos_lint_contract` requires alongside it) and the restored
  normalization that the module-ceiling trim had dropped from `__post_init__`.
  The second was a real regression the existing suite could not see, because
  the test claiming to prove normalization reached it through the factory that
  normalizes on its own.
- [x] (2026-09-27 13:50Z) Re-run of the failed gates against the fixed tree.
  `make lint` now runs to completion and passes: `python-lint` green (ruff,
  interrogate 100.0%, pylint 10.00/10, df12-pylint, ambrleaks, and **skylos
  with no findings**), then `rust-lint` (`lint-clippy` rustdoc + clippy under
  `-D warnings`, `lint-whitaker` across three crates, `spelling`) and
  `github-actions-lint` (`yamllint --strict`, `actionlint`) — so the two
  sub-checks the abort had left unobserved are now observed passing, and
  `actionlint` did not stall. `check-fmt` green (683 files formatted).
  `typecheck` green after the `ty: ignore` on the direct-construction row.
  `make test` then ran to completion, exit 0 in 3m43s, with all nine
  `PYTEST_TARGETS` patterns observed — pattern 1 `2543 passed, 63 skipped`,
  then 638, 2, 116, 4, 124, 12, 21, and 22 — and `test-rust` reaching nextest
  `125 tests run: 125 passed` plus the doctest leg. The aborting sweep's "1
  failed, 2542 passed" figure covered only the first pattern, so it was never a
  statement about the suite; the pattern with the failing contract test is now
  green, and no `|| exit $$?` abort occurred. The per-pattern lines are the
  log-backed evidence — a single suite-wide total is not printed by any one
  command. `typos.toml` was byte-identical before and after the run, and all
  four code files matched their pre-run hashes.
- [x] (2026-09-27 19:30Z) The round-3 review's one `major`, discharged by
  reading and then *measuring*: `_pipe_or` rewrote every stream in `pipes` to
  `PIPE`, ignoring the computed value, so an unread pipe target's `DEVNULL`
  left the parent holding a pipe nobody read and a child writing past the
  buffer hung in `write`. Measured here rather than argued: the kwargs reaching
  `create_subprocess_exec` carried `stdout == -1` (`PIPE`) where the spawn
  layer had computed `-3` (`DEVNULL`); a 16 MiB child with `capture=False` hung
  past 20 s on the fallback while the wait4 control finished in 0.02 s; the
  host's pipe capacity is 65536 bytes. Committed as `2c01835c`.
- [x] (2026-09-27 19:30Z) `_pipe_or` removed rather than narrowed, on evidence
  that it had no remaining work and its stated rationale was false. Enumerating
  its domain (54 value × stream × pipes combinations) showed the narrowed
  predicate was an identity function everywhere, because `_output_stdio`
  already returns exactly the int each backend needs. The docstring justified
  the helper by claiming `create_subprocess_exec` "will wrap whatever
  non-`None` value it is given in a pipe object"; a probe and asyncio's own
  `base_subprocess.py` (`if stdout == subprocess.PIPE: self._pipes[1] = None`)
  both show it attaches a reader only to the `PIPE` sentinel — a raw descriptor
  reaches the child with `Process.stdout` left `None`, exactly as `Popen` does.
  `origin/main` passes all three values straight through with no helper at all.
  Two docstrings carried the same false premise (`DirectProcessConfig`,
  `spawn_direct_process`) and are corrected; the latter also regained the
  `Returns` section ruff DOC required.
- [x] (2026-09-27 19:30Z) The regression test is red-capable, verified by
  injection rather than assumed. Its first draft asserted only after awaiting
  the child, so the red surfaced as a 30 s pytest-timeout that stranded a
  blocked child — the wrong reason. Reordered to assert the value the backend
  was handed *before* the await: red now fails in ~0.1 s naming `got -1` against
  `-3`, and a `finally` that kills and awaits collects whatever the failure
  leaves. Green against the restored fix in 0.49 s.
- [x] (2026-09-27 19:30Z) Round-3 typecheck failure, caught by `scrutineer` and
  fixed: the interceptor helper was annotated `**kwargs: int | None` and
  splatted into `create_subprocess_exec`, so `ty` checked all 19 of that
  function's keyword parameters against `int | None` (19
  `invalid-argument-type` diagnostics at one line). A narrower `**kwargs` is
  rejected at the splat and a wider one would have to be `Any`, which `ANN401`
  forbids here, so the five keywords the fallback actually sends are now named
  explicitly. Re-run sweep: all six gates green, tree unchanged before and
  after, no `typos.toml` churn.
- [x] (2026-09-27 21:15Z) Round-4 review found the borrowed-file flush
      misplaced;
  probes confirmed it was a real defect, not a style preference. The flush ran
  in `_stdio_plan._resolve_stdio_binding`, i.e. at *resolution* time, but
  resolution and the fork are the same instant only on the `run()` path:
  `lines()` resolves when it is called and forks at first iteration. Measured
  before fixing, with a borrowed stderr written once before the `lines()` call
  and once after: the child read `b'first '` while the file held
  `b'first second '` — the second write was dropped. Fixed by carrying the
  borrowed object on `_StdioBinding` and flushing in a new
  `_subprocess_spawn._flush_borrowed_stdio` immediately before the spawn; after
  the fix the child reads `b'first second '`. Regression test
  `test_borrowed_file_object_written_before_iteration_reaches_the_child` added,
  red-first verified by reverting the fix (the child then reads only
  `['caller-first']`). Also fixed in this round: the users-guide's default
  output description (settled by a `capture=False` probe, not by reading the
  resolver), the execplan's non-executable opening example (verified by running
  it: exit 0, `ALPHA BETA GAMMA` in `out.log`), the EP-M2 `connect_pipes()`
  description, and four places that cited the old flush location or a
  planned-but-unbuilt state shape; the roadmap rename finding was declined
  because the field it calls removed still exists.
- [x] (2026-09-27 18:40Z) Round-5 and round-6 review rounds, then round 7, which
      re-opened the memory claim for the second time — and found it reached
      production docstrings no round had named. Round 7 returned 7 findings from
      the `review --agent` pass on `8aefbbcf`; two pairs share a subject, so
      they reduce to 4 distinct issues, all correct. Three were documentation
      accuracy and one, the memory claim, was substantive: the round-6 wording
      bounded retention by "the pipe, not one chunk", which still drops the
      chunk — `_write_chunk` binds `chunk` and encodes `payload` and both stay
      live across `await sink.stdin.drain()`. Applying the round-5 lesson (grep
      the *concept* after falsifying a premise, rather than the sentence the
      reviewer quoted) surfaced two production docstrings that carried the
      *original* single-chunk claim and that neither the reviewer nor round 6
      had flagged: `cuprum/sh/execution.py` (`StdinStream`) and
      `cuprum/sh/safe_cmd.py` (`SafeCmd.run`'s `stdin` parameter), both
      introduced by `19938020b` on this branch. The mechanism is measured, not
      narrated: at `8aefbbcf` the round-6 replacement pattern `bounded by the
      pipe` occurs 0 times in both files while `peak memory` occurs once in
      each, so a search for a claim's *replacement* is structurally incapable
      of finding its predecessors. Also corrected: the users-guide event
      reference (per-chunk `stdin` events and the `early_close` operation
      value) and the developers-guide stdin narration (`_write_stdin_stream`
      dispatch via `_spawn_stdin_writer`, and `_await_exit_or_writer_failure`
      on the non-streaming path). All three Python edits were proved
      docstring-only by stripping docstrings with `ast` and comparing dumps.
- [x] (2026-09-27 18:40Z) The `594a0fee` sweep came back red on one gate, and
      the red is worth keeping. Six of seven gates passed; `make markdownlint`
      failed with exactly 2 `MD049/emphasis-style` errors at
      `docs/users-guide.md:310`, both from my own new text: `*ahead*` written
      into a file that uses underscore emphasis throughout. Measured with a
      grep for an asterisk-delimited span whose neighbouring characters are not
      asterisks, `docs/users-guide.md` carries 1 such span at `594a0fee` — the
      offending line — and 0 at `ac2b8c29`, against 164 underscore-delimited
      spans there. The commit was never pushed; the whole of the delta to
      `ac2b8c29` is that one line, `*ahead*` to `_ahead_` (`git diff --stat
      594a0fee ac2b8c29` reports `1 file changed, 1 insertion(+), 1
      deletion(-)`).
      The lesson: `make markdownlint` is not run by local `check-fmt` or
      `lint`, so a Markdown emphasis mistake reaches a full sweep only if the
      sweep includes it — which is why every sweep in this plan runs all seven
      rather than the formats the diff "should" touch.
- [x] (2026-09-27 17:05Z) The round-7 reply is posted to PR #511 as
      `issuecomment-5858015233`, after a full seven-gate re-sweep of
      `6346cbed` — the head that carries the two plan-only commits
      (`bd7297e4`, `6346cbed`) on top of the reviewed `ac2b8c29`. The re-sweep
      was not ceremony: `ac2b8c29`'s own sweep is archived at
      `/tmp/gates-445/prior-sweep-ac2b8c29/`, and `git diff --name-status
      ac2b8c29 6346cbed` shows the execplan is the only file that moved, but
      "nothing that matters moved" is a guess until the gates are run against
      the frozen tree. All seven exited 0; pytest's main session read `2545
      passed, 63 skipped in 172.87s` and nextest `125 tests run: 125 passed, 0
      skipped`, both identical to the reviewed head.
- [x] (2026-09-27 17:05Z) Two numbers in my own prose were wrong and were
      caught by re-measuring rather than by re-reading. (a) The reply's table
      first quoted the reviewed head's `test` duration as `159.60s`; that
      figure belongs to the `bd7297e4` sweep, and `ac2b8c29`'s archived log
      says `142.28s`. (b) The two columns of that table quoted *different
      lines* of the same `check-fmt` output — `683 files already formatted`
      (ruff) against `78 files left unchanged.` (mdtablefix) — which read as a
      discrepancy between heads when both logs in fact print both lines.
      Neither error would have been visible by reading; both fell out of
      putting the archived log beside the claim.

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
- Observation (lints): Skylos cannot see a dataclass's implicit caller, and the
  scope of that blindness is narrower than it first appears. After the
  CodeScene ordering fix made `StdioTarget.__post_init__` a three-call
  dispatch, the dead-code gate reported all three callees as unused
  (`cuprum/sh/stdio.py:48`, `:56`, `:76`), even though the dataclass machinery
  invokes the method after every `__init__`. Three rule shapes were probed:
  naming `__post_init__` itself as a `type = "method"` entry point, naming it as
  `type = "function"`, and naming only the callees. Only the third cleared the
  findings. Evidence: the JSON probe's `whitelisted` list carries the three
  names with `suppression_code: "configured_entrypoint"` and this rule's own
  reason text, `unused_functions` is 0, and `analysis_errors` is empty — the
  entries are attributed to *this* rule and not to a built-in "Enum member" or
  "Protocol class" suppression. A planted genuinely-dead function was still
  reported under the same rule, so it is not a blanket suppressor. The
  asymmetry is the interesting part: the dataclass-machinery edge is invisible
  to the reference graph, and naming the *dunder caller* does not restore it,
  so the callees are what must be named. Impact: the fix has two halves that
  must land together, because the repository already tests for exactly this —
  `test_skylos_lint_contract.py` freezes every entry-point name in
  `_RUNTIME_FUNCTION_ENTRY_POINTS` and fails on an unlisted addition. Adding
  the rule without the contract row turns `make test` red, and the contract
  test is the mechanism that forces the addition to be a reviewed decision
  rather than a silent suppression.
- Observation (verification): the `ty: ignore` added to a test row was
  load-bearing and is not removable as "obviously unnecessary". The row
  constructs `StdioTarget(kind="path", value=str(...))` directly to prove that
  `__post_init__` normalizes a `str` to a `Path`, but the field's declared
  union is `Path | int | IO[bytes] | IO[str] | None` — `StdioTarget.path()` is
  what wraps a `str` before the dataclass ever sees it, so the string spelling
  is deliberately outside the declared type. Evidence: `ty check` reports
  `error[invalid-argument-type]` on that exact argument with the ignore
  removed, and `All checks passed!` with it present.
- Observation (verification): removing a returned normalization can leave a
  test *green* while breaking the property it claims to test. The line-trimming
  refactor in the module-ceiling commit dropped
  `object.__setattr__(target, "value", Path(target.value))` from
  `__post_init__`, so a directly constructed path target stored a `str` and
  compared **unequal** to its own `Path` spelling built through the factory.
  The existing assertion at `test_safe_cmd_stdio_rules.py:148` already claimed
  to prove normalization, but reached it through `StdioTarget.path()`, which
  wraps in `Path` itself and so passed either way. Impact: the regression was
  invisible until a row constructed the dataclass directly. The
  ownership-comparison rule is where it would have surfaced in production:
  `_share_one_owned_path` decides whether stdout and stderr name one file by
  comparing *targets*, so an unnormalized `str` would have slipped past the
  shared-path refusal that exists to stop two independent offsets interleaving
  into one file.

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
- Decision: keep the `__post_init__` three-call dispatch rather than reverting
  it to the nested shape that CodeScene accepted, and pay for it with a Skylos
  entry-point rule. Rationale: the nested version put each variant's payload
  check inside the kind dispatch, which is what made the method a Complex
  Method with a Bumpy Road Ahead; the dispatch is the more readable of the two
  and the finding was real. The rule is the same remedy the repository already
  applies to its other implicit runtime callers, and `AGENTS.md` asks for a
  typed, explained entry point *before* a whitelist entry. Date/Author:
  2026-09-27, implementation agent.
- Decision: add the three names to `_RUNTIME_FUNCTION_ENTRY_POINTS` in
  `test_skylos_lint_contract.py` in the same commit as the rule that needs
  them, rather than relaxing that test. Rationale: the test is the mechanism
  that makes every entry-point addition a reviewed decision, so relaxing it to
  unblock this change would remove the only check standing between a real
  implicit caller and an unreviewed list entry. The row carries the reasoning
  inline, which is where a future reader will look. Date/Author: 2026-09-27,
  implementation agent.
- Decision: restore the `str`-to-`Path` normalization in `__post_init__`
  rather than drop the direct-construction test row that exposed its absence.
  Rationale: the row is the only thing in the suite that reaches normalization
  unfiltered, and the property it protects is load-bearing for
  `_share_one_owned_path`, which compares targets. Deleting the test would have
  returned the suite to the state where the regression was invisible.
- Observation (the round-3 major): a rewrite of a stdio value is invisible to
  every assertion made *after* the child is awaited. A pipe-buffer overrun on
  an unread pipe hangs, and a hang and a slow-but-correct run are reported the
  same way by anything that only inspects the final `CommandResult`. Only
  reading the kwargs the backend was handed, or timing the run, separates them.
  Impact: the regression test asserts the value first and awaits second, and
  the evidence that the defect was real is a recorded kwarg (`stdout == -1`)
  plus a timing pair (20 s hung vs 0.02 s control), not a passing suite.
- Observation (the round-3 major): the "cold" reading of a helper's docstring
  can be wrong in a way that survives review, because a plausible-sounding
  justification reads as verified. `_pipe_or`'s stated reason — that
  `create_subprocess_exec` wraps any non-`None` value, including a borrowed
  descriptor, in a pipe — is false, and CPython's `base_subprocess.py` says so
  in two lines. Impact: the helper was defended twice (once upstream on main's
  equivalent path, once by the branch) on a mechanism neither author checked.
  The probe that settled it was nine lines long.
- Observation (test hygiene): a regression test whose red phase times out is
  worse than one that fails, because the timeout path skips the assertions and
  leaves the process it created still blocked. The first draft here failed "for
  the wrong reason" in exactly that way. Impact: assert the cheap, decisive
  value before awaiting anything that a defect can make hang, and collect the
  child in a `finally`.
- Observation (typecheck): `**kwargs: X` on a helper that forwards to a
  many-parameter function is a typecheck trap, not a convenience. `ty` checks
  every parameter of the target against `X`; here that produced 19 diagnostics
  from one line, and `ANN401` closed the obvious escape (`Any`). Impact:
  helpers that forward to a wide signature should name the keywords they
  actually pass. Date/Author: 2026-09-27, implementation agent.
- Deviation (recorded, not silently absorbed): the plan's `Tolerances` bound
  this work at 24 tracked files or 1400 net lines, and the delivered change is
  materially larger — 44 tracked files and 5436 net added lines at `aca77867`,
  46 files and ~5600 net with the working tree. The split is 24 production
  files (+2122 net), 13 test files (+1708), and 7 documents (+1606). Rationale
  for proceeding rather than escalating: the overrun is not scope creep but the
  cost of the plan's own constraints. Three of the four milestones were
  required to *split* modules to stay under the 400-line ceiling, and each
  split has to re-home its own docstrings and tests; the four new test modules
  exist because the acceptance criteria ask for the refusals, the ownership
  lifetimes, and the bounded-memory bound to each be exercised separately; and
  the ExecPlan itself is 1200+ lines because the skill requires it to be
  self-contained enough for a novice to resume. The tolerance was written
  before that cost was visible, and its trigger — "if implementation requires
  editing more than 24 tracked files" — was in fact met early and repeatedly.
  It is recorded here as a deviation for review rather than retroactively
  rewritten to fit the outcome. Date/Author: 2026-09-27, implementation agent.
- Decision: delete `_pipe_or` rather than keep it in the narrowed form the
  reviewing agent asked for. Rationale: the narrow form is an identity function
  on every input its single call site can produce — proven by enumerating the
  54-point domain, not by inspection — so keeping it would preserve a call, a
  docstring, and two `PipeStream` parameters that do no work. Its justification
  was also false: `create_subprocess_exec` does not wrap a raw descriptor in a
  pipe, so the case the helper existed to prevent cannot arise. `origin/main`
  reaches the same conclusion by passing the three values straight through.
  Date/Author: 2026-09-27, implementation agent.
- Decision: keep the fallback spawning the child even when the value-display
  assertion has already failed, via a `finally` that kills and awaits, rather
  than returning early. Rationale: an early return would leave the blocked
  child running past the test, and the suite's whole subject here is process
  lifetime; a regression test that strands a child to prove a point about pipes
  would contradict the module docstring that says each test spawns a real child
  for exactly that reason. Date/Author: 2026-09-27, implementation agent.
- Decision: accept the round-3 finding that the roadmap's `_stdin_stdio` rule
  was imprecise, the execplan opening example did not run, and the CHANGELOG
  rejection wording named the wrong predicate; reject the other two findings.
  Rationale: the three accepted ones were each verified false against the tree
  and are fixed above. The users-guide finding ("do not present `pipe()` or
  `inherit()` as `StdioTarget` options for stdin") is contradicted by the code
  — a probe confirms both are accepted and the error message for `path()` reads
  `choose ['inherit', 'pipe']` — so the guide's existing sentence is already
  correct. The `_posix_only` finding asks to skip three `StdioTarget.path`
  tests on non-POSIX platforms; those tests open no borrowed descriptor and the
  file is absent from `EXTENSION_TEST_TARGETS`, which is the *only* thing the
  single Windows job (`extension-tests-windows`) runs, so the marker would be
  inert there while risking a genuine skip. Date/Author: 2026-09-27,
  implementation agent.
- Decision: move the borrowed-file flush from
  `_stdio_plan._resolve_stdio_binding` to a new
  `_subprocess_spawn._flush_borrowed_stdio`, carrying the caller's file object
  on `_StdioBinding` so the flush can happen at the fork. Rationale: a round-4
  review finding claimed the eager flush was misplaced, and a probe confirms it
  is a real defect rather than a style preference. Resolution and the fork
  coincide only on the `run()` path; `lines()` resolves its bindings when it is
  *called* and forks at first *iteration*, so a caller who writes to the
  borrowed object in between had those bytes silently dropped — the probe's
  child read `b'first '` while the file held `b'first second '`. The finding's
  proposed remedy (flush "in the spawn path, such as `_open_owned_stdio` or
  `_spawn_subprocess`") is what was implemented; its framing of `_StdioBinding`
  as "data only" is not, because the descriptor alone cannot flush a buffer and
  dropping the object from the binding would put the flush and the fork back
  out of step. Date/Author: 2026-09-27, implementation agent.
- Decision: correct the users-guide's `StdioTarget.pipe()` description to say
  an unobserved stream defaults to `/dev/null`, not inherited, and name
  `inherit()` as the explicit way to pass an output stream through. Rationale:
  a probe with `capture=False` and both streams unset produced *no* output on
  the parent's terminal, while an explicit `inherit()` on both produced it, so
  the guide said the opposite of what the library does. This reverses an
  earlier rejection of the same class of finding (round 3), which had been
  argued from `_resolve_output_binding`'s code rather than from a run; the
  measurement is the authority. Date/Author: 2026-09-27, implementation agent.
- Decision: decline the roadmap finding to rename `_ExecutionState.stdin_data`
  to `stdin_plan`. Rationale: the field was never renamed — `grep` finds
  `stdin_data: bytes | StdinStream | None` in `_command_internals.py`, and the
  roadmap bullet already says the field was *widened* and that the plan is
  resolved "from the field" in `_subprocess_spawn`. The finding's premise ("the
  removed `_ExecutionState.stdin_data` field") is false. Date/Author:
  2026-09-27, implementation agent.
- Deviation (recorded): the Plan of work section described the shape planned at
  EP-M2 — `stdin_plan` on the state, `stdout_target`/`stderr_target` fields, a
  `_resolve_stdio_target` helper, and the spawn mapping living in
  `_subprocess_execution.py` — none of which is what shipped. The implemented
  shape resolves the plan and the bindings together in
  `_build_subprocess_execution`, keeps the field name, and puts the spawn
  mapping in `_subprocess_spawn.py` with `_resolve_output_binding`/
  `_resolve_stdio` as the resolvers. Milestone plans are written before the
  code; the reconciliation that should have followed is what this entry
  supplies, and the section above now describes the delivered shape.
  Date/Author: 2026-09-27, implementation agent.
- Decision: correct the explanation of why `pipes` exists, in six sites across
  `cuprum/_stdio_plan.py`, `cuprum/_subprocess_spawn.py`,
  `cuprum/_wait4_process.py`, the ADR, and this plan. Rationale: a round-5
  finding flagged the claim that a `Popen` or `create_subprocess_exec` stream
  object is non-`None` "for anything it was handed, including a borrowed
  descriptor", and probes confirm the claim is simply false. Measured: `Popen`
  with `stdout=` a raw fd gives `Popen.stdout is None`; with a file object,
  `None`; with `PIPE`, a `BufferedReader`. `create_subprocess_exec` is
  identical for all four inputs — `None`, `None`, `StreamReader`, and `None` for
  `DEVNULL` — so the two backends do not diverge here at all. The real reason
  `pipes` is needed is the other half of the same fact: because `DEVNULL` also
  yields `None`, and the resolution folds a non-consuming pipe down to
  `DEVNULL`, the child object cannot distinguish a pipe cuprum owns from a
  borrowed descriptor. The prose in those six sites had the right conclusion
  (carry pipe-ness explicitly) attached to a wrong reason. That reason had
  already been caught once in its `create_subprocess_exec` form — the Progress
  entry at line 480 records a probe disproving "will wrap whatever non-`None`
  value it is given in a pipe object" — but the mirrored `Popen` claim was left
  standing, which is the lesson: when a premise is disproved on one backend,
  the same premise stated about the other has to be re-tested rather than
  assumed to hold. The `connect_pipes` guard's `stream in self._pipes` clause
  is a consequence: a probe deleting it left all 51 relevant tests passing,
  since the `Popen` `is not None` test alone already excludes `DEVNULL` and
  borrowed descriptors. It is kept as defence-in-depth for the config's `pipes`
  default, and the docstring now says plainly that the converse case cannot
  arise. Date/Author: 2026-09-27, implementation agent.
- Decision: widen the validation criterion for capture/echo/`on_line` to say the
  features work on streams that *stay piped*, and that a run combining one of
  them with a redirect on the same stream is rejected at construction.
  Rationale: a round-5 finding questioned the plan's "continue to work in
  combination with redirection" phrasing, and a probe confirms the rejection is
  real. `RunOutputOptions(capture=True, stdout=<path>)` and the `echo=True`
  equivalent both raise a message beginning "cannot be redirected" and ending
  "there is no parent-side pipe to read", while `capture=False` with the same
  redirect is accepted. The old wording described a combination the library
  deliberately refuses. Date/Author: 2026-09-27, implementation agent.
- Decision: sharpen the INV-3 after-final-chunk non-vacuity note to require a
  producer that terminates *normally* past its final chunk. Rationale: a
  round-5 finding asked for this, and a probe of a three-chunk producer shows
  the writer calls `__anext__` four times, the fourth raising
  `StopAsyncIteration` on the normal path — so a producer that *raises* when
  advanced past the end is not a contrast to the fault case, it is the fault
  case again. The note now names the one-step-past behaviour so the control
  cannot be built from the wrong fixture. Date/Author: 2026-09-27,
  implementation agent.
- Decision: replace the "parent never retains more than one chunk" memory claim
  wherever it appears. Rationale: a round-6 finding questioned it, and the
  source settles it. `_write_stdin_stream` awaits `sink.stdin.drain()` per
  chunk, and `StreamWriter.drain()` returns once the transport's write buffer
  has fallen to its low-water mark (`asyncio.streams._DEFAULT_LIMIT`, 64 KiB),
  not once the child has read — and beneath it the OS pipe holds bytes of its
  own (64 KiB by default on Linux). Several chunks are therefore legitimately
  in flight, and the plan's own test module had already said so: the negative
  control's docstring calls the cap "deliberately loose" and attributes it to
  "the pipe capacity … a property of the host rather than of cuprum". The prose
  asserted a bound the tests were explicitly written to avoid pinning. The
  claim became "bounded by the pipe, not by the input, and not by one chunk" in
  each site that made it. That replacement was itself incomplete, and round 7
  caught it: it named the transport and the pipe but dropped the chunk, which
  `_write_chunk` holds as both `chunk` and `payload` until `drain()` returns.
  The claim is now "the bound is on input pulled ahead, not on retained bytes:
  the largest chunk yielded, plus the transport buffer, plus the pipe" — see
  the entry below. Applying the lesson recorded for round 5, I grepped the
  concept rather than the flagged sentence, which is how two unflagged sites
  came to light. Date/Author: 2026-09-27, implementation agent.
- Decision: rewrite `INV-1` and strike two overclaims from it that the round-6
  finding did not name. Rationale: the obligation promised (a) a "configured
  buffering bound" that exists nowhere in the tree — the cap is a test constant,
  `_READ_AHEAD_CAP = _CHUNK_COUNT // 4` — and (b) a "peak-RSS check via the
  run's own resource measurement on the `wait4` path" that no test performs;
  `grep` for `resource_usage`/`rss`/`peak` in the stdin-stream suite returns
  nothing. Both would have read as evidence for properties nothing measures.
  `INV-1` now describes the delivered artefact: the pacing child's marker, the
  pull counter at first read, and a one-sided assertion against a pipe-derived
  cap — plus a closing paragraph stating plainly that there is no RSS
  measurement and no hard sub-pipe memory guarantee. Date/Author: 2026-09-27,
  implementation agent.
- Decision: complete the memory claim a second time, adding the chunk to the
  transport and the pipe, and apply it to two production docstrings the review
  did not flag. Rationale: round 7's four distinct findings are all one defect
  seen from different angles — the round-6 wording bounded retention by "the
  pipe, not one chunk", which is still wrong, because `_write_chunk` binds
  `chunk` and encodes `payload` and both stay live across
  `await sink.stdin.drain()`. The bound is a bound on *input pulled ahead*, not
  on what one step retains, and the type polices chunk size not at all. Two
  sites carried the original single-chunk claim and were caught by neither the
  reviewer nor round 6: `cuprum/sh/execution.py` (`StdinStream`) and
  `cuprum/sh/safe_cmd.py` (`SafeCmd.run`'s `stdin` parameter), both introduced
  by `19938020b` on this branch — so the defect was in code docs as well as
  prose, not documentation only. Applying the round-5 lesson again (grep the
  concept, not the flagged sentence) is what surfaced them; the round-6 grep
  used the *new* sentence as its pattern and so could not find the old one.
  Also corrected in this pass: the users-guide event reference, which omitted
  per-chunk `stdin` events and the `early_close` operation value, and the
  developers-guide stdin narration, which named only the payload writer and
  omitted `_await_exit_or_writer_failure`. Date/Author: 2026-09-27,
  implementation agent.

## Outcomes & retrospective

Delivered streaming stdin and explicit standard-stream redirection for
`SafeCmd`, as an additive widening that leaves the `StdinInput` payload API and
the inherited-stdin default unchanged. A caller can now hand a run an async
producer through `StdinStream(chunks=...)`, which is pulled one chunk at a time
and drained before the next pull, and can bind `stdout` or `stderr` to a
`StdioTarget` naming a library-owned pipe, the parent's stream, a file cuprum
opens before the spawn and closes in a `finally` immediately after, or a
borrowed descriptor cuprum never closes. Contradictory combinations — a variant
carrying the wrong payload, one owned path shared by two streams, capture or
echo alongside a redirected stream, a destination given as stdin — are rejected
at construction rather than at spawn, because each is a contradiction in the
caller's *intent* rather than a runtime condition.

All four milestones landed on green gate sweeps. The work added four focused
modules (`_stdio_plan.py`, `_subprocess_spawn.py`, `_subprocess_deadline.py`,
`_subprocess_stdin_stream.py`), split three that had crossed the 400-line
ceiling (`sh/stdio.py` out of `sh/output.py`, `sh/pipeline.py` out of
`sh/safe_cmd.py`, `_subprocess_stdin_stream.py` out of `_subprocess_stdin.py`),
and appended three dated addenda to ADR-007 rather than rewriting its accepted
text. Acceptance evidence is a byte-exact redirect test on both a zero and a
non-zero exit, borrowed descriptors shown to survive the run, a Hypothesis
property over generated `str`/`bytes` chunk lists with the child hexing its
stdin, and an INV-1 pull counter that measured 33 pulls of 256 read ahead of
the child against a cap of 64, with the eager negative control reading all 256.
That gap is the bound's whole content: 33 chunks were in flight together, which
is a pipe's worth of data rather than one chunk's.

Three lessons are worth carrying forward. First, a *cleared* ceiling is not a
stable state: the module-size plateau was re-crossed three separate times, each
by later work in the same feature, so the check belongs at every milestone
boundary rather than once. Second, a refactor that removes a line can leave the
suite green while breaking the property a test claims to prove — the
`str`-to-`Path` normalization was dropped from `__post_init__` by a
size-trimming edit, and the assertion that existed to catch it passed anyway
because it reached normalization through the factory that normalizes on its
own. A test must exercise the *narrowest* path to the behaviour it asserts.
Third, an aborted gate is not a failed gate: `make lint` stopping in
`python-lint` left `rust-lint` and `github-actions-lint` unobserved, and only a
re-run after the fix could distinguish "never ran" from "passed". The same
distinction recurred at a larger scale in `make test`, whose `|| exit $$?` loop
meant the aborting run's single failing pattern hid eight patterns and the
whole Rust suite.

Two defects found late are recorded in `Surprises & discoveries` rather than
quietly fixed: the Skylos dataclass-machinery blindness, which required naming
the `__post_init__` *callees* as entry points because naming the dunder caller
itself does not restore the reference-graph edge, and the normalization
regression above. Both were caught by gates and by a contract test the
repository already had for exactly this class of change.

The plan also ends with one unplanned outcome: the `Tolerances` scope bound was
breached, at 44 tracked files and 5436 net lines against a stated 24 files and
1400 lines. That is recorded as a deviation in `Decision log` rather than
absorbed silently. The short version is that the bound was set before the price
of the plan's own constraints was visible — three module splits forced by the
400-line ceiling, four separate acceptance test modules required by the
criteria, and a self-contained ExecPlan — and it was met early and repeatedly.
A future plan of this shape should set the file and line bounds after the
module-split itinerary is known, or scope the tolerance to *production* files
alone (24 files, +2122 net), which is the figure that actually tracks the
feature's cost.

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
   `stdout`, and `stderr` are today `int | None`, and `connect_pipes()`
   attaches a reader or writer only for streams named in the pipe set — the
   config carries one because `Popen` sets a parent-side stream only for the
   `PIPE` sentinel, leaving `DEVNULL` and a borrowed descriptor alike with
   `stdout`/`stderr` `None`, so pipe-ness cannot be read off the child object.
   When `wait4` is unavailable, plain `asyncio.create_subprocess_exec` is used
   instead.
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

ADR-007 keeps subprocess plumbing split across single-responsibility modules.
This plan **does** add module boundaries and move an existing one, so the ADR
was amended rather than satisfied by construction. Three append-only addenda
dated 2026-09-27 record the subprocess half: the stdin-producer split
(`_subprocess_stdin_stream.py`), the spawn-and-deadline split
(`_subprocess_spawn.py`, `_subprocess_deadline.py`, and the exit that
`_wait_for_exit_code_within_timeout` made from `_subprocess_wait.py`), and the
stdin-writer rendezvous (`_subprocess_rendezvous.py`). The accepted body above
them is left unedited: an addendum is appended, never a retroactive rewrite of
the text that was accepted. The boundaries are re-checked at each milestone
boundary.

The 400-line module ceiling is the practical expression of that ADR here. The
counts move as the work proceeds, so they are recorded by milestone rather than
restated here: at the time of writing `_subprocess_execution.py` is 381 lines,
`_subprocess_wait.py` 336, and `sh/safe_cmd.py` 337, all near enough to the cap
that new logic goes into new small modules rather than into them — which is how
the four modules above came to exist.

## Verification plan

Each invariant names the artefact that discharges it, the command that runs it,
and why a passing result cannot be vacuous.

- Obligation: `INV-1 — bounded retention`. A run whose producer yields far more
  than a pipe can absorb completes without the producer being drained ahead of
  the child, while the child consumes slowly. Method: integration test with a
  pacing child (a Python child that creates a marker once its first `read`
  returns) and a producer that counts how many chunks it has yielded by that
  moment; the cap is a quarter of the payload and roughly four times the Linux
  pipe capacity, and the assertion is one-sided against it. Rationale: the
  property is about retained state over time, which no single assertion
  captures; the pull counter at the child's first read is the observable proxy.
  Domain: 1 MiB of 4 KiB chunks. Artefact:
  `cuprum/unittests/test_safe_cmd_stdin_stream.py`. Evidence: `make test`; the
  test fails before EP-M3 with the producer fully consumed before the child
  read anything, and passes after. Non-vacuity: a deliberately eager
  implementation (collect the iterable into a list first) must cross the cap,
  and the test module includes that variant as a negative control so the
  assertion is shown to bite.

  Three limits of this obligation, stated so the evidence is not read as
  stronger than it is. The bound is on how far *ahead* the producer is pulled,
  not on what is retained: `drain()` returns once the transport's write buffer
  falls below its low-water mark, and the OS pipe holds bytes of its own, so
  several chunks are legitimately in flight — the test's cap is deliberately
  loose for exactly this reason. What is retained is therefore "the largest
  chunk yielded, plus the transport buffer, plus the pipe", not the pipe alone,
  and no chunk size is enforced: the type does not police it, so a producer
  yielding one enormous chunk is not protected from itself. And there is no
  peak-RSS measurement, on the `wait4` path or anywhere else: retained memory
  cannot be read off a finished run, so the counter stands in for it. A caller
  who needs a hard memory guarantee should yield bounded-size chunks, because
  cuprum offers none smaller than one chunk.

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
  after N chunks, for the two sites the suite builds: N = 0 (before the first
  chunk) and N = 1 (between chunks), written as `_raising_after(0)` and
  `_raising_after(1)`. Rationale: the failure must be raised on every exit
  path, and the exit paths are a finite enumeration. The two sites are chosen
  to be the two *distinct* outcomes rather than a longer series: N = 0 is
  failure known before the child ever exits, N = 1 is failure arriving after
  the child's exit has already settled. A producer that raises *after* its
  final chunk is constructible and is deliberately not a case here: it tests
  the fault the control below contrasts with, not the control. Domain: `run()`
  and `run_sync()`; capture on and off; the two raise sites above. Artefact:
  `cuprum/unittests/test_safe_cmd_stdin_stream.py`. Evidence: `make test`;
  before EP-M3 the exception surfaces as a bare producer exception or is
  swallowed, after it as `StdinSourceError` with the child's exit observed.
  Non-vacuity: the after-final-chunk case is the negative control — it must
  *not* raise, since the producer completed successfully; an implementation
  that wraps every exception indiscriminately fails that case. The control
  needs a producer that terminates *normally* after its final chunk and leaves
  the writer asking for one more: the writer always takes one step past the
  final chunk (a three-chunk producer's `__anext__` is called four times, the
  fourth raising `StopAsyncIteration` on the normal path), so a producer that
  raises when advanced past the end is not a control at all — it re-tests the
  fault case it is meant to contrast with.

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
than papered over: the test shows the writer does not pull far ahead of the
child for the exercised payload sizes, not that retained memory is bounded for
all conceivable schedules — and it measures pulls, not bytes, so a producer
yielding one enormous chunk would satisfy it while retaining that whole chunk.

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
  resolved `_StdioBinding` for each output stream, `_resolve_stdin_plan`,
  `_resolve_output_binding`, and `_resolve_stdio` (which returns the whole
  `_ResolvedStdio`). No single-target resolver is exported: the two output
  streams and stdin are resolved together, so a per-target helper would have no
  caller.
- `cuprum/_command_internals.py`: `_ExecutionState.stdin_data` keeps its name
  and is *widened* to `bytes | StdinStream | None` — the plan is resolved from
  it in `_build_subprocess_execution` rather than stored on the state, so the
  run's output targets travel to the spawn layer as part of the
  `_SubprocessExecution` bundle instead of as separate fields.
- `cuprum/_subprocess_spawn.py` (not `_subprocess_execution.py`): a new module
  owning the spawn's own surface. `_SubprocessExecution` carries the resolved
  `_ResolvedStdio`; `_spawn_subprocess` maps it to stdio values, opens owned
  paths immediately before the spawn call, and closes only the owned
  descriptors in a `finally` right after. `consumes_stdout`/`consumes_stderr`
  gain "and the stream is still a pipe", and `_flush_borrowed_stdio` performs
  the pre-fork flush that a resolver-side one could not place correctly (see
  the Decision log).
- `cuprum/_wait4_process.py`: `DirectProcessConfig` gains the pipe-ness
  information (a `pipes: frozenset[PipeStream]` field whose default preserves
  today's behaviour); `_Wait4Process.connect_pipes()` attaches a `StreamReader`
  only for streams named in it; the `create_subprocess_exec` fallback receives
  the same values, unchanged.
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

- Streaming a payload larger than a pipe can absorb while the child consumes
  slowly completes, and the producer's yield counter at the child's first read
  stays under a quarter of the payload — a bound of the pipe's making, not a
  one-chunk guarantee.
- Early pipe closure, producer failure, cancellation, and timeout each complete
  with no leaked writer task and no live child.
- A file target receives the child's exact bytes, and its descriptor is closed
  after the child was spawned — verified on normal exit, non-zero exit,
  timeout, and spawn failure.
- A borrowed descriptor and a borrowed file object are usable by the caller
  afterwards.
- Capture, echo, idle observation, and `on_line` continue to work on the
  streams that stay piped, and a run that asks for one of them *and* redirects
  that same stream is rejected at construction, with a message naming the
  fields. Redirection removes the parent-side pipe those features read, so the
  pair is unsupported rather than merely untested — the rejection is what makes
  `lines()` refuse a redirected stdout instead of silently observing nothing.

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
