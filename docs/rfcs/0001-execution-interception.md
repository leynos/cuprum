# RFC 0001: Execution interception for test doubles and passthrough

## Preamble

- RFC number: 0001
- Status: Proposed
- Created: 2026-09-27
- Author: raised from the leynos/lading adoption of `0.2.0b1` (lading roadmap
  phase 5, `leynos/lading#285`)

## Problem

Cuprum has no first-party way to run a `SafeCmd` without spawning a process.
Every seam it offers observes a real execution: `BeforeHook` and `AfterHook`
run either side of a spawn, `ExecHook` and the `observe_*` registrations
receive events from one, and `InMemoryMetrics` and `InMemoryTracer` record
telemetry from one. None of them can stand in for the child.

An adopter who wants to unit-test a caller of `run_sync()` therefore has three
options, and lading has now used all three:

1. Put a recording stub executable first on `PATH` and run a real process. This
   is what lading's release uploader tests do. It costs a process per case, a
   helper that isolates `GH_CONFIG_DIR`, `GH_HOST` and the credential variables
   so a misrouted call cannot reach a real account, and a `shutil.which` guard
   so a refactor that moves the stub off the front of `PATH` fails loudly. The
   helper is three hundred lines.
2. Keep cuprum out of the module under test by defining a port type of the
   caller's own (`CommandOutcome`, `UploadRunner`) and a one-function adapter
   that is the only importer of cuprum. This is sound hexagonal practice, but
   it means the type cuprum returns is never the type a policy module sees, and
   each adopter writes the same adapter.
3. Use a command-mocking framework that operates below cuprum, such as cmd-mox,
   which intercepts at the executable. That is the right tool for end-to-end
   behaviour, and lading will keep it. It cannot answer "what argv did cuprum
   build?" without a process, and it has no way to report to cuprum which
   invocation it served, so cuprum's own execution log and the mocking
   framework's journal are two records that must be reconciled by hand.

The same gap blocks lading's roadmap item 5.2.4, migrating cmd-mox
*passthrough*. When a stubbed command is configured to run the real thing,
lading's test runner resolves the executable itself, strips the shim directory
from `PATH` so the real binary is found rather than the shim again, applies a
per-test `CMOX_REAL_COMMAND_<NAME>` override, and then spawns directly.
Cuprum's catalogue model has no place to hand it a pre-resolved executable and
a pre-filtered environment for one invocation, so the passthrough path must
bypass cuprum entirely or re-implement the catalogue check around it.

## Current state

`SafeCmd.run()` and `run_sync()` in `cuprum/sh/safe_cmd.py` build a
`_SubprocessExecution` and await `_execute_subprocess()` in
`cuprum/_subprocess_execution.py`. That function takes the pre-spawn readings,
calls `_spawn_subprocess()`, which calls
`cuprum/_wait4_process.py:spawn_direct_process()` with a `DirectProcessConfig`,
and then drives capture, echo, idle monitoring and the hooks before assembling a
`CommandResult`. Pipelines spawn separately, through
`asyncio.create_subprocess_exec()` in `cuprum/_pipeline_spawn.py`, and share
the capture and pump plumbing in `cuprum/_pipeline_streams.py`.

Nothing between `run_sync()` and `spawn_direct_process()` is injectable.
`ScopeConfig` carries an allowlist, hooks, a timeout and an environment overlay;
`ExecutionContext` carries environment, working directory, sinks, encoding and
tags. Neither names the thing that will spawn.

## Goals and non-goals

Goals:

- Let a caller substitute the spawn for one scope or one call, receiving the
  fully resolved invocation (program, argv, environment, working directory,
  stdin, output options) and returning a `CommandResult` or an exception.
- Keep every existing seam, hook and result field working unchanged when the
  substitute is used, so a test sees the same `ExecEvent` sequence, the same
  `BeforeHook`/`AfterHook` calls, and the same `CommandResult` shape as a real
  run.
- Support passthrough: a substitute may decide, per invocation, to delegate to
  the real backend with an altered environment, or with a permitted programme
  resolved to a different path. Delegation is re-validated, so a substitute
  cannot widen what the scope allows.
- Ship a reference in-memory double in `cuprum.testing` that records
  invocations and returns scripted results.

Non-goals:

- Replacing cmd-mox, or any executable-level mocking framework. The seam is
  above the process; those tools sit below it, and both remain useful.
- Faking pipeline stage plumbing. The first iteration covers direct commands
  only; a pipeline whose scope has a substitute backend raises
  `NotImplementedError` with a clear message.
- Changing default behaviour. With no backend configured, nothing changes.

## Proposed design

### The backend protocol

```python
# cuprum/backend.py
@dc.dataclass(frozen=True, slots=True)
class Invocation:
    program: Program
    argv: tuple[str, ...]  # argv_with_program
    env: Mapping[str, str]  # fully resolved, after every overlay
    cwd: Path | None
    stdin: StdinInput | None
    output: RunOutputOptions
    context: ExecutionContext
    tags: Mapping[str, object]


class ExecutionBackend(Protocol):
    async def execute(
        self, invocation: Invocation, *, real: "ExecutionBackend"
    ) -> CommandResult: ...
```

`real` is always the production backend, so a substitute can delegate:
`return await real.execute(dc.replace(invocation, env=filtered))`. That single
parameter is what makes cmd-mox passthrough expressible: the substitute
resolves the executable, filters `PATH`, and hands the rest back.

Delegation is a *second* entry into the production path, and the allowlist
check that admitted the original invocation does not cover a replacement the
substitute substituted in. `_DirectBackend.execute` therefore re-checks the
programme it is given against the active scope before it spawns, and rejects a
delegated executable outside the allowlist with the same
`ForbiddenProgramError` a direct run raises. Without that, a passthrough that
rewrote `program` to an absolute path would withdraw the guarantee that a scope
runs only what it permits. A substitute that does not change the programme is
unaffected by the re-check.

### Where it is configured

- `ScopeConfig(backend=...)`, inherited by nested scopes like the allowlist and
  the hooks. Innermost wins.
- `run_sync(..., backend=...)` and `run(..., backend=...)` for one call.

The catalogue and scope allowlist checks run *before* the backend is consulted,
so the substitute sees only invocations cuprum would have spawned. That bounds
what it is offered, not what it may hand back: a substitute that returns a
result without delegating runs nothing, and a substitute that delegates can
change the programme on the way. The re-check in `_DirectBackend.execute`,
above, is what extends the original guarantee across the delegation boundary.

### Where it is called

`_execute_subprocess()` gains one branch: after the pre-spawn readings and
before the spawn, if a backend is configured it builds the `Invocation` and
awaits `backend.execute(invocation, real=_DirectBackend())`.

The events and the result are a second question, because in the current code
they do not sit outside the spawn. `_spawn_subprocess()` returns the process,
and `_execute_subprocess()` then emits `start`, waits, emits `exit`, and
assembles the `CommandResult` — every one of those steps reads `process.pid`,
`process.pid` having been the only source of the identifier. A substituted run
has no such object, so "the assembly stays where it is" cannot hold as written.

What moves and what does not:

- `_DirectBackend.execute` takes the spawn, the wait that follows it, and the
  two reads of the live process: `start` with the real `pid`, and the rusage
  measurement off the process object. None of these is producible without one,
  and the wait is what drives the streams, the timeout and the idle monitor.
- The `plan` event stays on the calling side. It is emitted before the backend
  is consulted and already carries `pid=None`, on all three entry points.
- The terminal `exit` event and the `CommandResult` are assembled by a shared
  helper on the calling side, from an outcome record the backend returns. That
  record carries an exit code, the exit instant, the captured streams, the CPU
  and RSS figures (or their absence), and, when the backend ran a child, the
  identifier it observed.

`_DirectBackend` returns `pid` from the process it spawned, which is what makes
a passthrough transparent. A backend that ran no process returns no identifier,
and the shared helper renders that as the existing `-1` "unavailable" sentinel
and `None` for the resource figures. Both paths then emit the same `exit` event
with the same field population, so the observable sequence is identical and the
"not measured" values carry exactly the meanings they already carry for a
platform without `wait4`.

### The reference double

```python
# cuprum/testing.py
class RecordingBackend:
    invocations: list[Invocation]

    def returns(self, *, exit_code=0, stdout="", stderr="") -> Self: ...
    def raises(self, error: BaseException) -> Self: ...
    def passthrough(self) -> Self: ...  # delegate to real
    async def execute(self, invocation, *, real) -> CommandResult: ...
```

Both blocks above sketch signatures, not final source; the constructor, the
scripted-result storage and the concrete parameter types are implementation
decisions. Scripted results are consumed in order; an unscripted invocation
raises `AssertionError` naming the argv, so a test cannot pass by accident.
This is deliberately small; matchers and fluent expectations belong to a
mocking framework, not to cuprum.

## Compatibility and migration

Additive. `ScopeConfig` and both run methods gain an optional keyword with a
default of `None`. `CommandResult` is unchanged; a substituted run reports
`pid=-1`, the documented "unavailable" sentinel `_execute_subprocess()` already
emits, and leaves `max_rss_bytes`, `user_cpu_seconds` and `system_cpu_seconds`
at their documented "not measured" value of `None`. No existing test needs to
change.

Adopters migrate opportunistically. lading would replace its `PATH`-stub
property tests with `RecordingBackend`, keep the stub helper for the two
end-to-end tests that genuinely exercise the process boundary, and route
cmd-mox passthrough through a small backend instead of bypassing the runner.

## Alternatives considered

- **Monkeypatch `spawn_direct_process`.** Works today, is private, and skips
  the environment resolution and hook sequencing a test usually wants to
  assert. It also cannot express passthrough with an altered environment
  without reproducing `DirectProcessConfig`.
- **A `dry_run=True` flag returning the argv.** Answers "what would run" but
  not "what does my caller do with a failure"; and it still leaves passthrough
  unaddressed.
- **Make `AfterHook` able to replace the result.** Hooks would then run for a
  process that never started, and the ordering guarantees in the users' guide
  would no longer hold. A hook is an observer; this is a substitute.
- **Recommend cmd-mox and stop.** It remains the recommendation for
  end-to-end tests. It does not help a policy module that should never spawn,
  and it does not solve the passthrough reconciliation problem, which is a
  cuprum-side gap.

## Implementation steps

1. Add `cuprum/backend.py` with `Invocation`, `ExecutionBackend`, and
   `_DirectBackend`; move the spawn and the two process reads of
   `_execute_subprocess()` into `_DirectBackend.execute()`, which re-checks the
   programme it is given before spawning, and return a normalized outcome
   record that the shared exit-event and result assembly consumes. Existing
   unit and behavioural suites must pass unchanged, and a delegation to an
   unpermitted programme must fail with `ForbiddenProgramError`.
2. Thread `backend` through `ScopeConfig`, the scope state, and the two run
   methods, with innermost-wins resolution and a property test that nesting
   behaves like the allowlist.
3. Add `cuprum/testing.py` with `RecordingBackend`, plus unit tests for the
   scripted-results order, the unscripted-invocation failure, and passthrough
   with a modified environment.
4. Add a behavioural scenario: a caller under test issues three commands, the
   double records all three with the resolved environment, and no process is
   created. The `pid` sentinel is part of what the scenario asserts, but it is
   not the evidence: `-1` is also what a run that spawned and failed to observe
   its child would report. Fail the scenario at the spawn boundary instead, by
   substituting the spawn entry point with one that fails the test if it is
   reached.
5. Raise `NotImplementedError` from the pipeline path when a backend is set,
   with a test.
6. Document the seam in the users' guide ("Testing callers without a process")
   and the scripting standards; add a changelog entry.
7. Downstream: lading 5.2.4 adopts the backend for cmd-mox passthrough and
   reports back; that report decides whether pipelines need the seam.

## Open questions

- Should the resolved `env` in `Invocation` be the full mapping or only the
  overlay delta? The full mapping is what a passthrough needs to filter; the
  delta is what a unit test usually asserts. Providing both (`env` and
  `env_overlay`) is cheap.
- Whether `RecordingBackend` should live in the main package or a
  `cuprum[testing]` extra. It has no dependencies, so the main package is
  simpler.
- Whether a substituted run should emit the idle-heartbeat events at all.

## Recommendation

Adopt the backend protocol with scope and per-call configuration, ship the
recording double, and defer pipelines. The change is additive, the moved code
is already isolated behind `_spawn_subprocess()`, and it removes the last
reason an adopter has to bypass cuprum in its own test runner.
