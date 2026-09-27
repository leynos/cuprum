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

`SafeCmd.run()` and `run_sync()` in `cuprum/sh/execution.py` build a
`_SubprocessExecution` and await `_execute_subprocess()` in
`cuprum/_subprocess_execution.py`. That function takes the pre-spawn readings,
calls `_spawn_subprocess()`, which calls
`cuprum/_wait4_process.py:spawn_direct_process()` with a `DirectProcessConfig`,
and then drives capture, echo, idle monitoring and the hooks before assembling a
`CommandResult`. Pipelines take a parallel path through
`cuprum/_pipeline_streams.py`.

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
  the real backend with an altered environment or executable path.
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
    argv: tuple[str, ...]          # argv_with_program
    env: Mapping[str, str]         # fully resolved, after every overlay
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

### Where it is configured

- `ScopeConfig(backend=...)`, inherited by nested scopes like the allowlist and
  the hooks. Innermost wins.
- `run_sync(..., backend=...)` and `run(..., backend=...)` for one call.

The catalogue and scope allowlist checks run *before* the backend is consulted,
so a substitute cannot be used to run a forbidden program; the substitute sees
only invocations cuprum would have spawned.

### Where it is called

`_execute_subprocess()` gains one branch: after the pre-spawn readings and
before `_spawn_subprocess()`, if a backend is configured it builds the
`Invocation` and awaits `backend.execute(invocation, real=_DirectBackend())`.
`_DirectBackend.execute` is the existing body of the function from the spawn
onwards, moved rather than duplicated. Hooks, events, telemetry and the result
assembly stay where they are, so a substituted run emits the same `plan`,
`start` and terminal events as a real one, with `pid=0` and no resource usage.

### The reference double

```python
# cuprum/testing.py
class RecordingBackend:
    invocations: list[Invocation]
    def returns(self, *, exit_code=0, stdout="", stderr="") -> Self: ...
    def raises(self, error: BaseException) -> Self: ...
    def passthrough(self) -> Self: ...          # delegate to real
    async def execute(self, invocation, *, real) -> CommandResult: ...
```

Scripted results are consumed in order; an unscripted invocation raises
`AssertionError` naming the argv, so a test cannot pass by accident. This is
deliberately small; matchers and fluent expectations belong to a mocking
framework, not to cuprum.

## Compatibility and migration

Additive. `ScopeConfig` and both run methods gain an optional keyword with a
default of `None`. `CommandResult` is unchanged; a substituted run fills
`pid=0`, `max_rss_bytes=None`, and the CPU fields with `0.0`, which are already
the documented "not measured" values. No existing test needs to change.

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
   `_DirectBackend`; move the post-spawn body of `_execute_subprocess()` into
   `_DirectBackend.execute()`. Existing unit and behavioural suites must pass
   unchanged.
2. Thread `backend` through `ScopeConfig`, the scope state, and the two run
   methods, with innermost-wins resolution and a property test that nesting
   behaves like the allowlist.
3. Add `cuprum/testing.py` with `RecordingBackend`, plus unit tests for the
   scripted-results order, the unscripted-invocation failure, and passthrough
   with a modified environment.
4. Add a behavioural scenario: a caller under test issues three commands, the
   double records all three with the resolved environment, and no process is
   created (assert on the `ExecEvent` `pid`).
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
