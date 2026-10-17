# Cuprum 0.2.0 migration guide

This guide helps an application that already uses Cuprum 0.1 move to Cuprum
0.2.0. It also has a short section for anyone who tested the 0.2.0-beta1
prerelease. Readers who are new to Cuprum need none of this: start with the
[users' guide](users-guide.md) instead.

Most of 0.2.0 is new, opt-in capability, but some changes need an edit before
an existing application works again. The guide puts those first, then
introduces the new features, so an upgrade can stop as soon as the application
works and come back for the rest later. Every Python example here is executed
by the project's test suite, so it runs as printed.

## Before you upgrade

Cuprum 0.2.0 is currently published as a beta, and installers skip prereleases
unless asked. Request it explicitly, and pin the exact beta in the
application's requirements so that moving to a later prerelease, which may
bring further breaking changes, is a deliberate choice:

<!-- shell-example: migration-install -->

```shell
python -m pip install "cuprum==0.2.0b2"
```

Then work through Table 1. Each row names a change, the code it affects, and
where to find the fix. A row that does not match the application can be skipped.

| Change                                                                                            | Affects code that…                                                |
| ------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| [`run(capture=, echo=)` moved to `RunOutputOptions`](#capture-and-echo-moved-to-runoutputoptions) | passes `capture=` or `echo=` to `run()` or `run_sync()`           |
| [Captured single-command streams are non-optional](#captured-single-command-results)              | coalesces result streams with `or ""` or checks the exact type    |
| [Scopes take a `ScopeConfig`](#scopes-take-a-scopeconfig)                                         | calls `scoped(allowlist=...)` or `CuprumContext.narrow(...)`      |
| [Disjoint nested scopes deny](#disjoint-nested-scopes-now-deny-everything)                        | nests scopes whose allowlists share no program                    |
| [Execution options are not child flags](#execution-options-are-no-longer-child-flags)             | passes `cwd=`, `env=`, `timeout=`, and similar to a builder       |
| [Builder arguments are typed](#builder-arguments-are-a-typed-contract)                            | passes `bytes`, lists, or other objects to a builder              |
| [`sh.make` uses the scoped catalogue](#shmake-resolves-the-scoped-catalogue)                      | calls `sh.make()` inside `scoped(catalogue=...)`                  |
| [Construct `ExecutionContext` by keyword](#construct-executioncontext-by-keyword)                 | passes four or more positional arguments to `ExecutionContext`    |
| [New execution event phases](#observe-hooks-see-new-event-phases)                                 | has an observe hook that rejects unknown `ExecEvent.phase` values |
| [Metrics move to the settled event](#metrics-count-failures-from-the-settled-event)               | builds dashboards on `cuprum_failures_total` or durations         |
| [`ExecHook` moved](#import-exechook-from-cuprum)                                                  | imports `ExecHook` from `cuprum.context`                          |
| [Echo is bounded and fails soft](#echo-is-bounded-and-no-longer-fails-on-encoding)                | echoes very long lines, or relied on `UnicodeEncodeError`         |
| [Environment overlays resolve at spawn](#environment-overlays-resolve-at-spawn)                   | relied on an overlay snapshotting `os.environ`                    |
| [Smaller changes](#smaller-changes)                                                               | uses `visible_settings()`, `IOOptions`, or internal module paths  |

_Table 1: Upgrade checklist for applications using Cuprum 0.1._

## Upgrading from 0.2.0-beta1

Beta testers already have most of 0.2.0. These changes arrived after beta1; the
first seven can need an edit:

- [Execution options are no longer child flags](#execution-options-are-no-longer-child-flags).
- [Builder arguments are a typed contract](#builder-arguments-are-a-typed-contract):
  `bytes` and other non-`ArgValue` values are rejected.
- [`sh.make` resolves the scoped catalogue](#shmake-resolves-the-scoped-catalogue).
- [Observe hooks see new event phases](#observe-hooks-see-new-event-phases):
  `settled`, `group_teardown`, and `stdio_error` are new since beta1.
- [Metrics count failures from the settled event](#metrics-count-failures-from-the-settled-event).
- The reserved builder keywords include `env_mode` and `process_group`, which
  are new `ExecutionContext` fields.
- [Captured single-command results](#captured-single-command-results) now use
  `CapturedResult` for default text capture; explicit `RunOutputOptions` calls
  keep the broader `CommandResult` type.

The rest is additive:

- [environment policies](#environment-policies);
- [executable bindings](#executable-bindings);
- [byte-exact capture](#byte-exact-capture);
- [the owned running-command handle](#owned-running-command-handle);
- [process-group ownership](#opt-in-process-group-ownership);
- [the broken-pipe policy](#opt-in-broken-pipe-policy-for-echoed-output);
- [control observation](#control-observation);
- [the Windows raw-handle outcome](#windows-raw-handle-hand-off-outcome);
- [streaming stdin and standard-stream redirection](#streaming-stdin-and-standard-stream-redirection);
- [redaction for telemetry](#opt-in-redaction-for-telemetry-projections); and
- the `py.typed` marker.

## Breaking changes

### Capture and echo moved to `RunOutputOptions`

In 0.1, `SafeCmd.run()` and `run_sync()` took `capture=` and `echo=` keywords.
Those keywords are gone, and passing them raises `TypeError`. Output settings
now travel together in one `RunOutputOptions` object, passed as `output=`:

<!-- tested-example: migration-run-output-options -->

```python
import io
import sys

from cuprum import ExecutionContext, Program, ProgramCatalogue, RunOutputOptions, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="output-options")
command = sh.make(Program(sys.executable), catalogue=catalogue)("-c", "print('hi')")

# 0.1: command.run_sync(capture=False, echo=True)
terminal = io.StringIO()
result = command.run_sync(
    output=RunOutputOptions(capture=False, echo=True),
    context=ExecutionContext(stdout_sink=terminal),
)
assert result.stdout is None and terminal.getvalue() == "hi\n"

try:
    command.run_sync(capture=False)
except TypeError:
    pass
else:
    raise AssertionError("the 0.1 keyword should be rejected")
```

`Pipeline.run()` and `run_sync()` still accept the old keywords for now, but
emit a `DeprecationWarning`; move them to `output=` as well. `IOOptions` is a
deprecated alias of `RunOutputOptions` and also warns.

### Captured single-command results

Default `SafeCmd.run()` and `run_sync()` calls, including explicit
`output=None`, now return `CapturedResult`, whose decoded streams are always
strings. An empty stream is `""`. Calls with explicit `RunOutputOptions` keep
the `CommandResult` type because those options can disable capture, even when
their particular value enables it.

Replace `result.stdout or ""` and `result.stderr or ""` with
`result.require_captured().stdout` and `.stderr`. Checked narrowing raises
`ValueError` when either text stream is unavailable, such as when capture was
disabled or when checking an interior pipeline stage. This preserves the
missing diagnostic instead of turning it into an empty string. `CapturedResult`
is a distinct dataclass, so exact type checks and cross-class equality checks
may also need updating; use `isinstance(result, CommandResult)` for base-result
checks.

### Scopes take a `ScopeConfig`

In 0.1, `scoped()` and `CuprumContext.narrow()` took keyword arguments such as
`allowlist=` and `before_hooks=`. Both now take a single `ScopeConfig`, which
gathers the allowlist, hooks, a default timeout, environment settings, a
catalogue, and executable bindings in one value. The old keywords raise
`TypeError`.

<!-- tested-example: migration-scope-config -->

```python
import sys

from cuprum import Program, ScopeConfig, current_context, scoped

PYTHON = Program(sys.executable)
started = []

# 0.1: with scoped(allowlist=frozenset([PYTHON]), before_hooks=(hook,)):
with scoped(ScopeConfig(allowlist=frozenset([PYTHON]), before_hooks=(started.append,))):
    assert current_context().is_allowed(PYTHON)
    assert not current_context().is_allowed(Program("rm"))
```

When the scope only needs the programs of a catalogue, `scoped(catalogue=...)`
is shorter; see
[`sh.make` resolves the scoped catalogue](#shmake-resolves-the-scoped-catalogue).

### Disjoint nested scopes now deny everything

In 0.1, an empty allowlist meant "allow everything", including the empty
intersection produced when a nested scope allowed only programs its parent did
not. A nested scope could therefore widen its parent by accident. A nested
scope now always narrows: when the intersection is empty, nothing may run.

<!-- tested-example: migration-disjoint-scopes -->

```python
from cuprum import Program, ScopeConfig, current_context, scoped

GIT, TAR = Program("git"), Program("tar")
with scoped(ScopeConfig(allowlist=frozenset([GIT]))):
    with scoped(ScopeConfig(allowlist=frozenset([TAR]))):
        # 0.1 allowed both here; 0.2.0 allows neither.
        assert not current_context().is_allowed(GIT)
        assert not current_context().is_allowed(TAR)
```

The default context, outside any scope, still permits every program. Code that
genuinely needs to widen a scope must say so with `allow()`, which the
[users' guide](users-guide.md#apply-a-policy) describes.

### Execution options are no longer child flags

Every builder keyword used to become a child `--name=value` argument, so
`python("-c", script, cwd=repo_dir)` built `--cwd=<path>` rather than changing
the child's working directory. A subprocess user's first four guesses — `cwd`,
`env`, `timeout`, and `stdin` — were all accepted and forwarded, and a tool
that ignores unknown flags ran in the wrong place with no error at all.

The builder now rejects every `ExecutionContext` field name and every
`run_sync` parameter with a `TypeError` that names the right call:

<!-- tested-example: migration-reserved-execution-options -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="reserved")
python = sh.make(Program(sys.executable), catalogue=catalogue)
try:
    python("-c", "print('unused')", cwd="/tmp")
except TypeError as exc:
    assert str(exc) == (
        "cwd is an execution option; pass ExecutionContext(cwd=...) to run_sync"
    )
else:
    raise AssertionError("cwd should not become a child flag")
```

Move the value to `run_sync`, passing `context=ExecutionContext(cwd=...)`,
`timeout=...`, or `stdin=...`. A command line that genuinely takes such a flag
still gets it positionally: `python("--cwd=<dir>")`. The rejection is narrower
than it sounds — only the exact names are reserved, so `working_dir=...` and
`stdin_file=...` continue to render as flags. See
[Execution options are not child flags](users-guide.md#execution-options-are-not-child-flags)
for the full list of reserved names.

### Builder arguments are a typed contract

`sh.make()` builders and the `build_argv` helper now accept exactly
`str | int | float | bool | pathlib.Path | os.PathLike[str]`, published as the
`ArgValue` alias. `sh.make` returns a `SafeCmdBuilder` whose signature states
that domain, so a call passing anything else is rejected by a static checker as
well as at run time.

Previously the builder stringified any object with `str()`. A list, a dict, or
an arbitrary object therefore reached the command line as its `repr` — a
silently wrong argument rather than an error. `bytes` fared worst: `b"release"`
was passed to the child as the literal text `b'release'`. Values outside
`ArgValue` now raise `TypeError` naming the offending type or value; `None`
keeps its own message.

Migrate a call that was relying on that stringification by passing text (or a
path) yourself:

<!-- tested-example: migration-typed-arguments -->

```python
from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs("echo", name="typed-arguments")
builder = sh.make(Program("echo"), catalogue=catalogue)

# A list used to become "['a', 'b']"; join it into the text you meant.
tags = ["a", "b"]
command = builder("--tags", ",".join(tags))
assert command.argv == ("--tags", "a,b")
```

Decode caller-held filesystem bytes with `os.fsdecode()` before building the
command:

<!-- tested-example: migration-bytes-arguments -->

```python
import os
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="bytes-arguments")
python = sh.make(Program(sys.executable), catalogue=catalogue)
argument = os.fsdecode(b"v\xc3\xbf")
command = python("-c", "print('two words')", argument)
assert command.argv == ("-c", "print('two words')", argument)
```

`os.fsdecode()` uses the configured filesystem encoding and error handler; see
[Exactly which values are accepted](users-guide.md#exactly-which-values-are-accepted)
in the users' guide for the platform differences. Custom path-like objects are
accepted: a class defining `__fspath__` that returns text is converted with
`os.fspath()`, so it behaves exactly as a `pathlib.Path` does. Only a
`__fspath__` returning `bytes` is rejected.

### `sh.make` resolves the scoped catalogue

`scoped(catalogue=catalogue)` derives the scope's allowlist from the
catalogue's programs, and also activates the catalogue for `sh.make()`, so call
sites inside the block may drop the repeated `catalogue=` argument. Keep using
`scoped(ScopeConfig(...))` when the scope also needs hook or policy
configuration.

Review call sites that build inside a catalogue scope for a different
catalogue, or that rely on `DEFAULT_CATALOGUE` while a catalogue scope is
active: `sh.make()` now resolves the scoped catalogue and raises
`UnknownProgramError` at construction when it does not list the program. The
ready-made builders in `cuprum.builders`, such as `git_rev_parse()`, resolve
the same way, so inside a catalogue scope that catalogue must list the tool.
Pass an explicit `catalogue=` argument where a different catalogue is the
intent. The two enforcement points are described under
[Two enforcement points](users-guide.md#two-enforcement-points).

### Construct `ExecutionContext` by keyword

`ExecutionContext` gained fields, and two of them, `native_pump_cleanup_grace`
and `timeout`, sit straight after `cancel_grace`. A call that passed four or
more positional arguments, such as `ExecutionContext(env, cwd, 0.5, sink)`, now
puts the fourth into a different field. Name every argument instead:
`ExecutionContext(env=env, cwd=cwd, cancel_grace=0.5, stdout_sink=sink)`.

### Observe hooks see new event phases

An observe hook registered with `sh.observe()` receives every `ExecEvent`. In
0.1 the phases were `plan`, `start`, `stdout`, `stderr`, and `exit`. 0.2.0 adds
`stdin`, `stdin_error`, `stdio_error`, `timeout`, `teardown_error`,
`capture_eof_grace_expired`, `pipeline_fail_fast`, `group_teardown`, and
`settled`. A hook that matches exhaustively on `ExecEvent.phase` and raises on
an unknown value now fails the run, because Cuprum re-raises observe-hook
failures. Give such hooks a default arm:

<!-- tested-example: migration-exhaustive-hook -->

```python
import sys

from cuprum import ExecEvent, Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="phases")
python = sh.make(Program(sys.executable), catalogue=catalogue)
outcomes = []


def hook(event: ExecEvent) -> None:
    match event.phase:
        case "settled":
            outcomes.append(event.terminal_outcome)
        case _:
            pass  # Phases this hook does not track, including future ones.


with sh.observe(hook):
    python("-c", "pass").run_sync()
assert [outcome.value for outcome in outcomes] == ["exit_zero"]
```

`settled` is the one most hooks will want. It is emitted once for each
execution whose `plan` event began observation, including spawn failure,
timeout, cancellation, and other errors. `terminal_outcome` is one of
`exit_zero`, `exit_nonzero`, `timeout`, `cancelled`, or `error`. The event
reuses the plan's `exec_id`. It includes a `pid` and `exit_code` only when
those child details are known; a spawn failure has neither, and no exception
text is included. Catalogue and allowlist validation still happen before
observation and produce no lifecycle events. `exit` remains the event for
actual child status and resource data.

`group_teardown` reports a teardown outcome Cuprum could not resolve, such as a
signal the kernel refused; see
[Cleanup diagnostics](users-guide.md#cleanup-diagnostics). `stdio_error`
reports a failed standard-stream step, such as a stdin producer that raised,
alongside the exception the caller receives. See
[structured execution events in the users' guide](users-guide.md#structured-execution-events)
for every phase and field.

### Metrics count failures from the settled event

The bundled adapters now use `settled` rather than `exit` for terminal
accounting. In `MetricsHook`, `cuprum_failures_total` and
`cuprum_duration_seconds` are recorded from `settled`, so
`cuprum_failures_total` now counts every outcome except `exit_zero` — a
timeout, a cancellation, and a spawn failure included — where 0.1 counted only
a non-zero exit. `cuprum_failures_total` and `cuprum_executions_total` also
carry `env_mode` and `capture_mode` labels. `TracingHook` closes its spans on
`settled`. Review dashboards and alerts built on these series; the
[metrics adapter reference](users-guide.md#metrics-adapter) lists them all.

### Import `ExecHook` from `cuprum`

The `cuprum.context.ExecHook` re-export has been removed. Import `ExecHook` from
`cuprum` or from its definition site, `cuprum.events`. Only the import path
changes, not the hook signature or registration behaviour.

### Echo is bounded and no longer fails on encoding

Two echo behaviours changed. Captured output is unaffected by either.

- Each echoed line is limited to 64 KiB by default, and longer lines are cut
  with a truncation marker. Pass `RunOutputOptions(max_echo_line_bytes=None)`
  for the 0.1 unbounded echo.
- A text-only sink that cannot encode the child's output, such as a Windows
  CP1252 console receiving UTF-8, no longer raises `UnicodeEncodeError` out of
  the run. Echo stops for that stream, capture completes, a warning is logged,
  and the result records the fallback; see
  [Echo-fallback diagnostics](#echo-fallback-diagnostics).

### Environment overlays resolve at spawn

Scoped `env(...)` overlays resolve against the live `os.environ` when each
child is spawned, not against a snapshot taken when the scope began. Code that
relied on a snapshot should pass explicit values through the overlay or
`ExecutionContext.env` instead.

### Smaller changes

- `ProgramCatalogue.visible_settings` is now a property. The callable spelling
  `visible_settings()` still works during the migration period, but prefer
  attribute access.
- `CommandResult` now lives in `cuprum.sh.results` and `Pipeline` in
  `cuprum.sh.pipeline`. Both remain importable from `cuprum` and `cuprum.sh`,
  which are the supported paths.
- `SafeCmdBuilder` is now a protocol rather than a `Callable[..., SafeCmd]`
  alias.
- Cuprum now ships a `py.typed` marker, so type checkers read its annotations.
  A downstream type check may report errors it could not see before.

## New in 0.2.0

The features below are opt-in: an application that does not use them behaves as
it did. Several are large enough that the users' guide teaches them from the
start:

- Deadlines with `timeout=` and input with `StdinInput`; see
  [Supply input, environment, and a deadline](users-guide.md#supply-input-environment-and-a-deadline).
- Concurrent execution with `run_concurrent()`; see
  [Run commands concurrently](users-guide.md#run-commands-concurrently).
- Validated builders for git, rsync, and tar in `cuprum.builders`; see
  [Wrap commands in project builders](users-guide.md#wrap-commands-in-project-builders).

The sections that follow cover the remaining additions.

### Single-project catalogue construction

`ProjectSettings.documentation_locations` and `noise_rules` now default to
empty tuples. Callers whose project has no documentation references or output
noise rules can omit both fields:

<!-- tested-example: migration-catalogue -->

```python
from cuprum import Program
from cuprum.catalogue import ProgramCatalogue, ProjectSettings

settings = ProjectSettings(name="rust-test-gates", programs=(Program("cargo"),))
catalogue = ProgramCatalogue.from_project(settings)
assert catalogue.lookup(Program("cargo")).project_name == "rust-test-gates"
```

Callers with project metadata can continue to pass `documentation_locations=`
and `noise_rules=` explicitly. When a complete `ProjectSettings` is already
available, `from_project()` removes the repeated
`ProgramCatalogue(projects=(settings,))` wrapper; existing catalogue
construction remains compatible. `ProgramCatalogue.from_programs()` builds a
single-project catalogue straight from programs, as the users' guide examples
do.

### Line-level output observation

`SafeCmd.lines()` and the `RunOutputOptions.on_line` callback observe decoded
stdout and stderr lines as they arrive. Existing callers using `SafeCmd.run()`
or pipeline execution require no changes.

`SafeCmd.lines()` yields `LineEvent` values with the stream name, decoded text,
and monotonic arrival time. Its `LineStream.result` attribute exposes the
completed `CommandResult`. `RunOutputOptions.on_line` delivers the same event
payload synchronously during `run()` for callers that do not need an async
iterator. Capture and echo remain independent of line observation, including
when both are disabled.

For pipelines, line observation covers the final stage's stdout and every
stage's stderr. The stdout of an interior stage feeds the next stage and is
therefore not emitted as a line event. See the
[line-level output section in the users' guide](users-guide.md#line-level-output)
for ordering, timestamps, teardown, and capture/echo details.

### Owned running-command handle

`SafeCmd.start()` returns a `RunningCommand` handle rather than a
`CommandResult`. The addition is opt-in: `run()`, `run_sync()`, and `lines()`
keep owning their child from start to finish.

The handle owns the child's lifetime. Leaving the handle's `async with` block
settles the run on every exit and stops a still-running child down the same
`SIGTERM`, cancel-grace, and `SIGKILL` path a cancelled `run()` takes. Capture
is always on for a handle, so a stopped child still reports the output it
produced. A `timeout` passed to `start()` is the default deadline for each
`wait()` on that handle rather than a limit on the child's life, because the
clock a timeout is measured against starts at the wait that asks for one.

The handle exposes `wait()`, `signal()`, `terminate()`, and `kill()`. `wait()`
awaits the outcome, which is also readable from `result` once the handle has
settled. `terminate()` stops the child with `SIGTERM` and the cancel grace
before `SIGKILL`; `kill()` compels it with `SIGKILL` alone; both are no-ops on
an already-exited child. `signal()` delivers one POSIX signal without settling
the run. Every caller shares the one settlement, so a later `wait()` reports
the same recorded result rather than starting the run again.

`signal()` has two refusals, because the remedy differs:

- `SignalAfterExitError` means the child has already exited: treat the
  recorded outcome as the command's ending.
- `UnsupportedSignalError` means the platform has no route to a caller-named
  signal; use `terminate()` or `kill()`, which use the platform's own signals.

For a worked example, the wait deadline, and both refusals, see
[hold a running child](users-guide.md#hold-a-running-child-and-decide-when-to-stop-it)
in the users' guide.

### Control observation

Control actions on a running child — `signal()`, `terminate()`, and `kill()` on
a `RunningCommand` — can be observed separately from the command lifecycle.
Register a hook with `observe_control()` to receive a `ControlEvent` for each
one, recording whether the child complied, had to be killed, or the request was
refused; nothing is observed unless a hook is registered. The
`cuprum.adapters.control_metrics` module counts these events. See
[Other observation channels](users-guide.md#other-observation-channels).

### Byte-exact capture

`SafeCmd.run_bytes()` and `Pipeline.run_bytes()`, with `run_bytes_sync()`
counterparts, return the child's output undecoded. These entry points are
additive: `run()` and `run_sync()` keep decoding by default and keep returning
`CommandResult` and `PipelineResult`.

A byte run is the same run in a different result mode. It accepts the same
`output`, `timeout`, and `context` arguments (plus `stdin` for a single
command) and returns `BytesCommandResult` or `BytesPipelineResult`, whose
output fields are `bytes | None` rather than `str | None`. Each stage of a
`BytesPipelineResult` is a `BytesCommandResult`. All measurement fields — `pid`,
`started_at`, `duration`, `max_rss_bytes`, `user_cpu_seconds`,
`system_cpu_seconds`, and `relay_fallbacks` — are unchanged, so nothing is lost
by capturing exactly.

Two behaviours are worth noting before adopting it:

- `RunOutputOptions.on_line` is rejected with `ValueError` before the child is
  spawned. The capture is byte-exact, so a callback carrying decoded text would
  contradict it. Structured observation registered with `sh.observe()` is
  unaffected and still receives decoded lines.
- A timeout still raises `TimeoutExpired` and still carries the partial output
  read so far, in the same field, as bytes rather than text. An external
  cancellation still re-raises `asyncio.CancelledError` rather than being
  converted into a timeout.

A byte-exact run's line observation and echo render a _view_ of the child's
bytes rather than reporting them. Undecodable input is replaced in those views
rather than left to raise, so an ambient `sh.observe()` hook cannot end a
`run_bytes()` from the read loop and cost it the bytes it had already captured.
This is the one place `ExecutionContext.errors` does not reach, and it is
scoped to the byte-exact mode. Text mode keeps its policy: `run()`,
`run_sync()`, and `SafeCmd.lines()` under `errors="strict"` still raise
`UnicodeDecodeError` on undecodable output.

Callers who previously worked around decoding by round-tripping through a
surrogate encoding can now drop it and request the byte mode directly. See the
[binary output section in the users' guide](users-guide.md#binary-output) for
the full contract and a worked example.

### Executable bindings

`bind_executable()` separates a program's catalogue identity from the file that
runs. A caller that binds nothing keeps running each catalogued program under
its own name, and the new `resolved_path` field is `None` on every `ExecEvent`
and `CommandResult`.

Before, the only way to run a pinned path was to register the path _as_ the
program, which made that path the catalogue identity, the policy key, and the
telemetry label. Now the binding is a scope and the identity is untouched:

<!-- tested-example: migration-executable-bindings -->

```python
import sys

from cuprum import Program, ProgramCatalogue, ScopeConfig, bind_executable, scoped, sh

PYTHON = Program("python")
catalogue = ProgramCatalogue.from_programs(PYTHON, name="migration-bindings")
python = sh.make(PYTHON, catalogue=catalogue)

with (
    scoped(ScopeConfig(allowlist=frozenset([PYTHON]))),
    bind_executable(PYTHON, sys.executable),
):
    result = python("-c", "import sys; sys.stdout.write(sys.executable)").run_sync()
    assert result.program == PYTHON, "the catalogue identity is preserved"
    assert result.resolved_path == sys.executable, "the bound path ran instead"
```

A binding does not grant permission. If a caller's program is inside the
allowlist it runs; if it is outside, it is refused with
`ForbiddenProgramError`, and the binding's resolver is never called for it.
Nested scopes override one program at a time and leave sibling bindings intact,
so introducing a binding cannot silently change what a neighbouring scope runs.

A resolver must return a `str`; anything else is rejected, which keeps `None`
meaning only "no binding". At an execution boundary, any resolver failure — a
wrong return type, a virtual environment that was never created, a toolchain
that is not installed — surfaces as `ExecutableResolutionError` (a
`RuntimeError`, imported from `cuprum.executable_binding`), naming the logical
program with the original exception chained as `__cause__`. Callers migrating a
hand-rolled resolver should note that a `FileNotFoundError` raised inside one
no longer escapes as itself. A malformed path is rejected when the binding is
made, with `InvalidExecutableBindingError`, a `ValueError`.

Two limits apply. Path validation is advisory — construction rejects malformed
paths, but the file at the bound path may be replaced between the check and the
`exec`, so filesystem ownership, write permissions, and read-only deployment
remain the operator's responsibility. And `resolved_path` is a _string_ the run
reports, not a hash or a descriptor: it names the executable that was started,
but it does not attest to the bytes at that path. See
[§5.1.2 of the design document](cuprum-design.md#512-executable-bindings) for
the full boundary.

### Environment policies

`EnvMode`, `UNSET`, and the `env_mode` fields on `ScopeConfig`,
`CuprumContext`, and `ExecutionContext` are opt-in; import `EnvMode` and
`UNSET` from `cuprum.context`. Existing callers keep the environment-overlay
behaviour and require no changes. Use `EnvMode.REPLACE` only where a child must
receive an explicitly supplied environment, and use `UNSET` to remove an
inherited variable from an overlay. See
[Choose how a child environment is composed](users-guide.md#choose-how-a-child-environment-is-composed).

### `CommandResult` execution measurements

`CommandResult` now exposes `started_at` as a wall-clock timestamp and
`duration` as elapsed monotonic seconds. The fields default to `0.0`, so code
that constructs a result with the existing six positional arguments remains
compatible. Results returned by command execution include measured values.

On Linux and macOS, a direct command's sole child-reap owner uses `wait4` to
obtain that child's user CPU time, system CPU time, and maximum RSS. Linux
`ru_maxrss` is converted from KiB to bytes; macOS reports bytes directly. RSS
is not calculated by subtracting process-global `RUSAGE_CHILDREN` snapshots. On
platforms without the child-specific wait interface, CPU fields may use the
aggregate POSIX fallback and are approximate when commands run concurrently;
maximum RSS is unavailable. Windows and platforms without child-resource
accounting return `None` for all three resource fields. Pipeline stages also
return `None` for all resource fields because concurrent child reaping cannot
attribute usage safely to an individual stage.

Consumers that serialize or display these optional fields should preserve
`None` as unavailable and should treat `user_cpu_seconds` and
`system_cpu_seconds` as approximate when the aggregate fallback is in use.

### Echo-fallback diagnostics

`CommandResult` exposes handled text-sink echo failures through its
`relay_fallbacks` tuple. Each `RelayFallback` contains the affected stream and
the closed error category, `unicode_encode` or `broken_pipe`, so consumers can
count or report per-command fallbacks without parsing log messages. Pipeline
stage results expose the records owned by that stage, with stdout records
before stderr records.

The field is trailing and defaults to `()`, so existing six-argument positional
construction and existing keyword construction remain compatible. Commands that
time out or are cancelled do not produce a result-level diagnostics tuple;
their already-emitted echo events remain available through `observe_echo`. The
warning, echo event, and result record carry only bounded categorical values
and never include output, sink details, exception objects, or command arguments.

### Opt-in broken-pipe policy for echoed output

A presentation sink whose destination closes under the run raises
`BrokenPipeError` from the echo drain. `RunOutputOptions.broken_pipe_policy`
defaults to `BrokenPipePolicy.STRICT`, which propagates the error and aborts
the run, so an application that does not opt in behaves as before.

To adopt the policy, name it on the run's output options:

<!-- tested-example: migration-broken-pipe -->

```python
import sys

from cuprum import (
    BrokenPipePolicy,
    Program,
    ProgramCatalogue,
    RunOutputOptions,
    sh,
)

catalogue = ProgramCatalogue.from_programs(sys.executable, name="broken-pipe")
command = sh.make(Program(sys.executable), catalogue=catalogue)("-c", "print('hello')")
options = RunOutputOptions(broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT)
assert options.broken_pipe_policy is BrokenPipePolicy.BEST_EFFORT
result = command.run_sync(output=options)
assert result.ok and result.stdout == "hello\n"
```

Under best-effort, a `BrokenPipeError` from a presentation sink disables echo
for the affected stream only, while capture, line observation, child reaping,
and result diagnostics continue: the command still returns a result, and that
result's `relay_fallbacks` names the `broken_pipe` category. Only
`BrokenPipeError` is affected; any other sink `OSError` still propagates under
both policies. `BrokenPipePolicy` is exported from the package root next to
`RunOutputOptions`.

### Idle heartbeat for quiet children

`RunOutputOptions.idle_after` defaults to `None`, so the heartbeat is off by
default: a run with no interval creates no timer and no watchdog task. When set,
`idle_after` is a strictly positive, finite number of seconds of silence on
the monitored streams before a notification is due. Further notifications
repeat once per further interval of silence, and any non-empty read on a
monitored stream resets the timer.

To adopt the heartbeat, set the interval on the run's output options:

<!-- tested-example: migration-idle -->

```python
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="idle")
command = sh.make(Program(sys.executable), catalogue=catalogue)("-c", "print('done')")
result = command.run_sync(output=RunOutputOptions(idle_after=30.0))
assert result.ok and result.stdout == "done\n"
```

By default, the built-in renderer writes one flushed, newline-terminated,
at-most-512-byte, ASCII-safe line to `ExecutionContext.stderr_sink`, falling
back to `sys.stderr`, for example
`[cuprum] still running cargo (idle 30s, total 4m10s)`. That line is never
written into captured stdout or stderr, into child-output line observers, or
into the activity tracker. The heartbeat is an observation only: it reports the
absence of observed output, and never diagnoses a deadlock, terminates a
process, or extends a timeout.

A pipeline uses one aggregate clock over the parent's outward-facing output:
the final stage's stdout and every stage's stderr. Inter-stage transfers do not
reset it, and its line is labelled `pipeline output idle`.

`on_idle(elapsed_total, elapsed_idle)` replaces the built-in renderer rather
than joining it. It is called synchronously on the run's own event loop, so it
must not block. The sink the built-in renderer writes to is written and flushed
on that same loop as well, so a blocking `write` or `flush` delays the parent's
stream reads, timeout handling, and cancellation. Wrap a slow sink so that the
write and flush hand off without blocking: run the blocking call in a worker
thread or an executor, or use a genuinely non-blocking drain such as a queue
fed with `put_nowait`.

<!-- tested-example: migration-queue-sink -->

```python
import queue


class QueueSink:
    """Feed a queue that something off the run's loop drains."""

    def __init__(self, pending: queue.Queue[str]) -> None:
        self._pending = pending

    def write(self, text: str) -> None:
        self._pending.put_nowait(text)

    def flush(self) -> None:
        pass


pending: queue.Queue[str] = queue.Queue()
sink = QueueSink(pending)
sink.write("ready")
sink.flush()
assert pending.get_nowait() == "ready"
```

A separate asyncio task is not enough because draining that queue still runs on
the run's own loop. An ordinary exception from the callback, or a failed
diagnostic write, disables further notifications for that run, emits one
sanitized warning, and leaves the child's exit status and captured output
untouched. `KeyboardInterrupt` and `SystemExit` are never suppressed.

### Opt-in presentation sink

`cuprum.sinks.GitHubActionsSink` frames a run's echoed output in a GitHub
Actions collapsible log group and turns a failed run into one `::error::`
annotation. Nothing is framed unless a sink is passed, and capture, success
semantics, and the returned result are unchanged.

To adopt the sink, pass it through `RunOutputOptions` on the `SafeCmd` or
`Pipeline` to be framed:

<!-- tested-example: migration-actions-sink -->

```python
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh
from cuprum.sinks import GitHubActionsSink

catalogue = ProgramCatalogue.from_programs(sys.executable, name="actions")
command = sh.make(Program(sys.executable), catalogue=catalogue)("-c", "print('hello')")
result = command.run_sync(
    output=RunOutputOptions(echo=True, sink=GitHubActionsSink()),
)
assert result.ok and result.stdout == "hello\n"
```

`GitHubActionsSink` activates automatically when the parent process runs on
GitHub Actions, which is when `GITHUB_ACTIONS` holds the runner's value `true`.
For local reproduction of CI framing, or on a non-standard runner that does not
export the variable, pass `force=True` to activate the sink deliberately.

### Group and annotate flags

Two flags on `RunOutputOptions` reach the same framing without constructing a
sink, for callers who want collapsible groups and failure annotations and
nothing else:

<!-- tested-example: migration-group-annotate -->

```python
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh
from cuprum.sinks import GitHubActionsSink

catalogue = ProgramCatalogue.from_programs(sys.executable, name="migration-flags")
command = sh.make(Program(sys.executable), catalogue=catalogue)("-c", "print('hello')")
options = RunOutputOptions(echo=True, group=True, annotate_failure=True)
assert isinstance(options.sink, GitHubActionsSink)
result = command.run_sync(output=options)
assert result.ok and result.stdout == "hello\n"
```

Both flags default to `False`, and unflagged output is byte-for-byte what it
was. `group=True` frames the run in a `::group::` / `::endgroup::` pair with a
stop-commands lease; a pipeline emits one group for the whole pipeline, matching
`GitHubActionsSink`. The two flags are independent: `annotate_failure=True`
alone emits the single `::error::` annotation on a non-zero exit, timeout, or
error and frames no group. An explicit `sink=` takes precedence and makes both
flags no-ops, so a shared options object carrying `group=True` never displaces
a sink chosen at the call site.

The flags synthesize a `GitHubActionsSink`, so they inherit its activation
gate: they take effect only when the parent process runs on GitHub Actions
(`GITHUB_ACTIONS == "true"`), and a run outside that environment behaves as if
they were absent. Passing anything but a `bool` for either flag raises
`ValueError`. Capture, echo destinations, exit codes, and the returned
`CommandResult` are unchanged in every case. The flags are a spelling of the
adapter decision in
[ADR-013](adr-013-opt-in-github-actions-presentation-sink.md), which records
why they construct the sink instead of teaching the execution layer workflow
commands. For custom destinations and titles, and deliberate local activation,
see the
[GitHub Actions presentation section](users-guide.md#present-output-in-github-actions)
in the users' guide, which covers the
[group and annotate flags](users-guide.md#group-and-annotate-flags) and
[direct sink configuration](users-guide.md#configure-the-sink-directly).

### Opt-in redaction for telemetry projections

Structured logging and tracing export the argument vector, and events can carry
environment values and output. To keep known secrets out of those exports,
build a `RedactionPolicy` from `cuprum.redaction` with the literal strings and
regular expressions to mask, and pass it as `redaction=policy` to each
exporting adapter: `structured_logging_hook()`, `tracing_hook()`,
`logging_hook()`, and `GitHubActionsSink`. Wrap any other observer with
`redacting_hook(observer, policy)`.

Nothing changes for an application that does not opt in. Redaction alters only
what is exported, never the command, its environment, or the captured result,
and a failing rule raises `RedactionError` rather than exporting raw text. See
[Keep secrets out of telemetry](users-guide.md#keep-secrets-out-of-telemetry)
for an example and the full rules.

### Opt-in process-group ownership

Cuprum terminates the process it spawns, but only that process. When the child
spawns descendants of its own, they are not signalled, and they can hold
inherited pipe descriptors open after the child is reaped — the case a timeout
most often creates, because the work a wedged command is waiting on is usually
a grandchild.

`ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP)` is an opt-in
policy for that. It defaults to `ProcessGroupPolicy.INHERIT`, which tears down
only the direct child, so a run that does not opt in behaves as before.

To adopt the policy, name it on the run's execution context:

<!-- tested-example: migration-process-group -->

```python
import sys

from cuprum import ExecutionContext, ProcessGroupPolicy, Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="process-group")
command = sh.make(Program(sys.executable), catalogue=catalogue)(
    "-c", "import os, sys; print(os.getpgrp() == os.getpid())"
)
owned = ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP)
assert owned.process_group is ProcessGroupPolicy.OWN_GROUP
assert command.run_sync(context=owned).stdout == "True\n"
# The default is unchanged: a run that says nothing keeps the caller's group.
assert ExecutionContext().process_group is ProcessGroupPolicy.INHERIT
```

Under `OWN_GROUP` the child becomes the leader of a new session and process
group, so its process-group identifier is its own PID. Teardown signals that
group, which reaches the child's descendants as well as the child itself. Under
`INHERIT` the child remains in the caller's group and teardown signals the
child alone, as it always has.

Session leadership also detaches the child from the controlling terminal. An
application that opts in should expect a terminal interrupt (`SIGINT`) or
`SIGHUP` to stop at the run rather than reach the child, and a child that opens
`/dev/tty` to fail for want of one. Under `INHERIT` the terminal still reaches
the child, so this is part of what the opt-in trades away rather than a
behaviour change to migrate around.

The guarantee is bounded, and the bound is worth stating plainly rather than
burying: a descendant that calls `setsid()` or `setpgid()` leaves the group
deliberately, and nothing here can reach it. Containment of that kind needs a
supervisor, a cgroup, or a container, which belongs to the caller rather than
to a command runner. Ancestors and siblings are outside the group for the same
reason.

`OWN_GROUP` is rejected with `ValueError` at spawn on Windows, which has no
POSIX process groups. The nearest equivalent, a Job Object, cannot be assigned
atomically with process creation, so containment there could begin only after
the child had a chance to spawn a descendant. Refusing is honest; accepting the
option and providing no containment would not be. `INHERIT` works on every
platform, and is the default there as everywhere else.

`ProcessGroupPolicy` is exported from the package root next to
`ExecutionContext`, so an existing import line such as
`from cuprum import ExecutionContext, sh` only needs `ProcessGroupPolicy` added
to it. The decision and its alternatives are recorded in
[ADR-021](adr-021-process-group-ownership.md).

### Aggregate Python stream-operation observation

An opt-in observation channel reports completed operations in the pure-Python
stream paths. No observer or metric is installed unless the application
registers one with `observe_stream_operation`.

To adopt the channel, register a synchronous hook around the command or
pipeline scope that should be observed:

<!-- tested-example: migration-stream-metrics -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics
from cuprum.adapters.stream_metrics import stream_operation_metrics_hook
from cuprum.stream_observation import observe_stream_operation

metrics = InMemoryMetrics()
catalogue = ProgramCatalogue.from_programs(sys.executable, name="metrics")
command = sh.make(Program(sys.executable), catalogue=catalogue)("-c", "print('hello')")
with observe_stream_operation(stream_operation_metrics_hook(metrics)):
    result = command.run_sync()
assert result.ok and result.stdout == "hello\n"
```

The hook receives one aggregate `StreamOperationEvent` for each completed
stream drain or pipeline transfer. Events include the closed operation and
outcome values, total bytes consumed, completed reader-operation count
(including EOF), monotonic duration, and any safely available existing
execution correlation. No event is emitted per read or per chunk.

The optional metrics adapter records byte and reader-operation counters and a
duration histogram. It uses only the closed `operation` and `outcome` labels;
payloads, read sizes, paths, process identifiers, and exception text are not
labels. Observer and collector failures are logged and suppressed, so enabling
observation does not change command or pipeline execution behaviour.

The registration is context-local. Remove the registration by leaving its
context manager or calling `detach()` on the returned handle. The existing
`ExecEvent` observation API is unchanged.

### Windows raw-handle hand-off outcome

`RustPumpHandoffOutcome` gains `WINDOWS_RAW_HANDLE_REJECTED`, recorded as
`windows_raw_handle_rejected` in `cuprum_rust_pump_handoff_total`. This bounded
outcome identifies deliberate rejection when a direct native-shim caller
supplies a raw Windows handle; normal pipeline hops still fall back to the
Python pump as before. Update dashboards and exhaustive outcome handling to
include the new value.

### Streaming stdin and standard-stream redirection

Two additions cover input and output that should not pass through memory. Both
are opt-in: an application that uses neither sees no change, and `StdinInput`
works as before.

- **Streaming input:** `stdin=StdinStream(chunks=...)` feeds the child from an
  async iterable of `str` or `bytes` chunks, pulling the next chunk only after
  the previous write has drained. A producer failure terminates the child and
  raises `StdinSourceError`, with the original exception chained. See
  [Stream input from a producer](users-guide.md#stream-input-from-a-producer).
- **Redirection:** `RunOutputOptions(stdout=..., stderr=...)` takes a
  `StdioTarget` to send a stream to a file (`path()`), a descriptor the caller
  owns (`fd()`), or the parent's own stream (`inherit()`), instead of capturing
  it. Contradictory combinations, such as capture with a redirected stream or
  an inherited stdin alongside a stdin source, raise `ValueError`, and file or
  descriptor targets need a POSIX platform. See
  [Send a stream to a file or the parent](users-guide.md#send-a-stream-to-a-file-or-the-parent).

`StdinStream`, `StdinSource`, `StdinSourceError`, and `StdioTarget` are
exported from the package root. Exhaustive observe hooks also see the new
`stdio_error` phase; see
[Observe hooks see new event phases](#observe-hooks-see-new-event-phases).

`RunOutputOptions.stdout` and `.stderr` accept a `StdioTarget`, which names
where one of the child's standard streams is bound. Its four variants differ in
_ownership_, which is the contract:

- `StdioTarget.pipe()` — a library-owned pipe cuprum creates, consumes or
  writes, and closes.
- `StdioTarget.inherit()` — the parent's own stream, passed straight through.
- `StdioTarget.path(path)` — a file **cuprum owns**. Cuprum opens it
  immediately before the spawn and closes its copy in a `finally` immediately
  after, so the descriptor never outlives the run. The caller names a path and
  never manages a descriptor.
- `StdioTarget.fd(fd)` — a **borrowed** descriptor or open file object the
  caller owns. Cuprum never closes it, and a borrowed file object is flushed
  immediately before the spawn so buffered caller-side bytes reach the child.

The lifetime contract follows from that split. Cuprum closes only what it
opened: a `path` target's file is opened before the spawn and closed right
after it, while a borrowed descriptor or file object is only flushed by cuprum.
A caller who reuses one borrowed descriptor across two runs shares a single
file offset, so the second run writes wherever the first left off.

Contradictory combinations are rejected. `RunOutputOptions` refuses the
combinations whose contradiction is visible once its three targets are
assembled; the inherited-stdin rule is checked when the run is prepared,
because whether a source was also supplied is not known when the options are
built:

- A stream that capture or echo must read cannot be redirected: there is no
  parent-side pipe to read.
- One path cannot be named for both stdout and stderr: each open starts at
  offset 0, so the two streams would interleave unpredictably.
- `RunOutputOptions.stdin` accepts only `StdioTarget.pipe()` and
  `StdioTarget.inherit()`. A `path` or `fd` target is refused, because the
  input itself arrives on the run call's `stdin=` argument.
- An inherited stdin alongside a stdin source is refused: both answer where
  the child's input comes from, and honouring one would silently discard the
  other. An explicit stdin pipe alongside a source is accepted, because the
  source is written through the pipe the target names.
- `Pipeline` rejects explicit standard-stream targets on all three streams;
  redirect a single `SafeCmd` instead.

Redirection through a `path` or `fd` target needs POSIX descriptor semantics:
on a platform without them, cuprum refuses the descriptor arms rather than
opening a stream and failing to hand it over. Inheriting a stream and a
library-owned pipe are not platform-limited, so a supported combination keeps
working there.

`StdioTarget`, `StdinStream`, `StdinSource`, and `StdinSourceError` are all
exported from the package root, so an existing import line only needs the new
name added to it. For the full contract, see
[capture and echo](users-guide.md#capture-and-echo) for the target vocabulary
and the rules policing it, and
[supply input, environment, and a deadline](users-guide.md#supply-input-environment-and-a-deadline)
for the streaming source.

### Per-stream capture policies

Capture was a single joint switch. `capture=True` retained both streams
completely, in the parent's memory, for as long as the result was alive, and
`capture=False` retained nothing. A command that produced more output than the
caller wanted to hold had to choose between an unbounded result and no result
at all.

Cuprum 0.2.0 adds a per-stream policy. `capture_stdout` and `capture_stderr`
accept a policy, and the `capture` shorthand still sets both at once, so
existing applications need no change and a run that says nothing behaves as
before:

<!-- tested-example: migration-per-stream-capture -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.capture_policy import BoundedCapture
from cuprum.sh import RunOutputOptions

catalogue = ProgramCatalogue.from_programs(sys.executable, name="capture-policy")
command = sh.make(Program(sys.executable), catalogue=catalogue)(
    "-c", "import sys; sys.stdout.write('abcdefghij'); sys.stderr.write('kept')"
)
bounded = RunOutputOptions(
    capture_stdout=BoundedCapture(max_bytes=4), capture_stderr=True
)
result = command.run_sync(output=bounded)
# stdout is the retained tail; stderr is untouched by the other stream's bound.
assert result.stdout == "ghij"
assert result.stderr == "kept"

# Each stream is bounded on its own, so the two policies coexist in one run.
by_stream = {record.stream: record for record in result.captures}
assert by_stream["stdout"].policy == "bounded"
assert by_stream["stdout"].dropped_bytes == 6
assert by_stream["stderr"].policy == "full"
assert by_stream["stderr"].dropped_bytes == 0

# The default is unchanged: both streams are captured in full.
default = command.run_sync(output=RunOutputOptions())
assert default.stdout == "abcdefghij"
assert default.stderr == "kept"
```

`BoundedCapture(max_bytes=N)` keeps the last `N` **bytes** in memory. The bound
is a byte count rather than a character count, and it never splits a code
point, so the tail it retains is always decodable and the count is what a
caller can reason about without knowing the encoding. The run also reports what
it dropped, on the new `captures` field of `CommandResult` and `PipelineResult`:

<!-- tested-example: migration-capture-metadata -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.capture_policy import BoundedCapture
from cuprum.sh import RunOutputOptions

catalogue = ProgramCatalogue.from_programs(sys.executable, name="capture-metadata")
command = sh.make(Program(sys.executable), catalogue=catalogue)(
    "-c", "import sys; sys.stdout.write('abcdefghij')"
)
result = command.run_sync(
    output=RunOutputOptions(
        capture_stdout=BoundedCapture(max_bytes=4), capture_stderr=False
    )
)
(record,) = result.captures
assert (record.stream, record.policy, record.storage) == ("stdout", "bounded", "memory")
assert (record.total_bytes, record.dropped_bytes, record.truncated) == (10, 6, True)
# An uncaptured stream contributes no record at all, rather than an empty one.
assert [entry.stream for entry in result.captures] == ["stdout"]
```

A bound applies to what the result keeps. It does not shorten a pipeline
stage's output on its way to the next stage — the relay is the child's own
pipe, which the parent never reads — and it does not shorten the mirrored copy
an echo sink receives. `max_echo_line_bytes` and a capture bound are separate
limits on separate paths.

`FileBackedCapture` is the policy for a stream too large to hold: it keeps the
stream on the heap until `spill_threshold_bytes`, then moves it to a file. In
text mode the payload for a spilled stream is `None` and the record names the
file, because reading the file back would defeat the point of spilling it; in
byte mode the file is read back, so byte mode does not carry the bounded-memory
guarantee. The file is removed by the run before it returns unless
`keep_file=True` is passed, on every exit route including a timeout and a
cancellation. Set `keep_file=True` if the caller wants the file, and read
`record.owned` to know which case applies.

A failure to create, write, or read back the spill file does not fail the run:
it is reported on the record as `storage_failed=True` and through a
`CaptureEvent`, because a failure to _retain_ output is not a failure of the
command that produced it. If the file could not be created the stream stays in
memory and is still reported complete; if an append failed the record holds
what reached the file before the failure; if a byte-mode read-back failed the
file is left in place for the caller to read. A failure to remove the file at
the end of the run is absorbed silently, so that cleanup cannot replace the
caller's own timeout or cancellation.

`BoundedCapture`, `FileBackedCapture`, `FullCapture`, `CapturedStream`, and
`CapturePolicyKind` are exported from the package root and from `cuprum.sh`, as
well as from `cuprum.capture_policy`. The full field list of a capture record
is in [Control output](users-guide.md#control-output).

## For contributors: benchmark and profiling changes

These two sections concern the harnesses in `benchmarks/`, not the `cuprum`
library. Applications that only use `cuprum` need no change.

### Aggregate stream telemetry in profiling reports

The tee hot-path profiling harness gained two keys, and both are additive: the
keys that were already present are unchanged, and the artefacts remain plain
JSON. No stream read behaviour changes.

- `stream_telemetry` in each scenario's `worker-result.json`, covering that
  worker run, including every repeat in its repeat loop.
- `stream_telemetry_summary` in a sweep's `read-size-sweep.json`, combining the
  telemetry of every completed sample in the sweep.

Both keys are written unconditionally, so every worker run and every sweep now
carries them. A report consumer that ignores unknown keys needs no change, and
a consumer that validates these artefacts strictly must accept the new keys.

Both keys use the same shape: a `groups` mapping keyed first by the closed
operation (`stream_drain` or `pipeline_transfer`) and then by the closed outcome
(`eof`, `cancelled`, `failed`, `downstream_closed`, or
`post_close_drain_timeout`), plus a `totals` group summed over every group
present. Each group carries four aggregate fields, and nothing else. The three
counters `bytes_consumed`, `read_operations`, and `operation_count` are
integers, and `duration_seconds` is a float summing monotonic operation
durations.

The telemetry observes only the pure-Python drain and pipeline-transfer paths,
so a `pipeline_transfer` group may be absent when the Rust backend handles a
hop; an absent group is not a failure. No per-read or per-chunk telemetry is
emitted. The aggregate shape mirrors
[aggregate Python stream-operation observation](#aggregate-python-stream-operation-observation),
the opt-in production channel described above.

### Benchmark ratchet measurement protocol

The `benchmark-ratchet` job now measures a single 64 MiB payload at five worker
iterations and twenty hyperfine runs, selected with `--ci-ratchet`. It
previously compared a Rust-to-Python ratio over payload tiers where interpreter
start-up, the `cuprum` import, and per-iteration set-up were most of both
means, so a runner-to-runner swing in that fixed cost could move the ratio past
the threshold on its own — the false positive reported against
[PR #158](https://github.com/leynos/cuprum/pull/158).

A local ratchet reproduction, or a comparison against recorded history, is
affected by the following changes:

- `BENCHMARK_PROFILE_VERSION` is now `pipeline-worker-release-ratio-v5`. The
  payload and iteration changes are sampling-protocol changes, so the version
  was bumped with them. The ratchet compares only samples whose profile
  metadata agrees, and a plan carrying the older version is refused rather than
  compared, so a stale plan must be regenerated instead of reused.
- `--ci-ratchet` defaults `--worker-iterations` to the count the job measures
  at. Omitting the flag on the sweep keeps the sweep's own count. An explicit
  `--worker-iterations` overrides either.
- `--ci-ratchet` and `--smoke` select contradictory payloads and are mutually
  exclusive.

While the rolling window refills with compatible `main` samples, the comparison
falls back to a single-sample bar where the flat threshold decides alone. The
workflow's `--max-regression`, `--noise-sigmas`, and `--history-window` values
are pinned to the defaults in `benchmarks/ratchet_history.py` by a CI contract
test, so a reproduction should not need to pass them explicitly.

For the full description, see
[the CI ratchet workload](developers-guide.md#the-ci-ratchet-workload) in the
developers' guide and `docs/cuprum-design.md` (§13.9), which also links
[the noise measurements](debugging/debugging-plan-2026-09-16-ratchet-overhead-noise.md)
behind the change.
