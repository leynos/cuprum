# Cuprum users' guide

Cuprum helps Python applications run a known set of external programs without
building shell command strings. This guide starts with a complete command, then
continues through output, failures, pipelines, policy, and production use. Each
Python example is executed from this document by the behavioural suite. The
[glossary](#glossary) at the end defines recurring terms.

## Choose a path

- **First command:** [Run a command](#run-a-command).
- **Existing subprocess caller:** [Migrate a caller](#migrate-a-caller) and the
  [0.2.0 migration guide](v0-2-0-migration-guide.md).
- **Working application:** [Handle failure](#handle-failure),
  [control output](#control-output), and [apply a policy](#apply-a-policy).
- **Several commands:** [Connect a pipeline](#connect-a-pipeline) or
  [run commands concurrently](#run-commands-concurrently).
- **Growing an application:** [Going further](#going-further) has task
  recipes for catalogues, builders, streaming, hooks, and telemetry.
- **Exact contracts:** the
  [operational reference](#operational-reference) lists events, metrics, log
  records, and backend behaviour.

The examples that launch a child use the current Python interpreter. This
avoids assuming that an optional executable such as git is installed.

## Install Cuprum

Cuprum requires Python 3.12 or newer. Install it with pip:

<!-- shell-example: install-pip -->

```shell
python -m pip install cuprum
```

Or add it to a uv project:

<!-- shell-example: install-uv -->

```shell
uv add cuprum
```

The pure Python installation has no runtime dependencies. On CPython 3.13 for
Linux, macOS, and Windows x86_64, pip may instead select a native wheel that
adds optional Rust acceleration for pipelines; behaviour is the same either
way. See [Optional Rust acceleration](#choosing-a-stream-backend) for details.

## Run a command

Declare the executable, make a builder, build an argument vector, then run it.
`sh.make()` consults a catalogue before returning a builder. The default
catalogue contains common tools such as `ECHO`, `GIT`, `LS`, `RSYNC`, and
`TAR`. For an application-specific executable, build a catalogue explicitly:

<!-- tested-example: first-command -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="my-script")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "print('hello, cuprum!')")
result = command.run_sync()
assert result.ok
assert result.stdout == "hello, cuprum!\n"
assert result.stderr == ""
```

`run_sync()` suits a synchronous script. In an async application, use
`await command.run()` inside an async function. Both return `CommandResult`.
Cuprum passes an argument vector directly to the child, without a shell: shell
quoting and pipes in a string are not interpreted.

## Handle failure

`UnknownProgramError` means `sh.make()` could not find a program in its
catalogue. That is a policy or configuration error. A registered executable
that is absent from the operating system raises `FileNotFoundError` at launch.
A child that exits non-zero returns a result with `ok == False` and an
`exit_code`; it does not automatically raise.

<!-- tested-example: failures -->

```python
import sys

from cuprum import Program, ProgramCatalogue, UnknownProgramError, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="failures")
try:
    sh.make(Program("not-in-this-catalogue"), catalogue=catalogue)
except UnknownProgramError:
    pass
else:
    raise AssertionError("an unknown program should be rejected")

python = sh.make(Program(sys.executable), catalogue=catalogue)
result = python("-c", "import sys; sys.exit(7)").run_sync()
assert not result.ok
assert result.exit_code == 7
```

Treat a non-zero result according to the called tool's exit-code contract.
Inspect `stderr` for diagnostics, but do not parse it to decide whether
Cuprum's catalogue accepted the program. For a missing executable, check the
path or the child's inherited `PATH`.

## Build arguments deliberately

The builder that `sh.make()` returns accepts strings, numbers, booleans, and
paths as positional arguments. Keyword arguments become `--name=value`;
underscores in names become hyphens. This suits tools that actually accept that
form. For flags such as `--check`, pass a positional argument: `check=True`
would produce `--check=True`. `None` raises `TypeError` in either position, so
decide whether to omit or substitute an optional flag before building. An
argument containing spaces remains one argument.

<!-- tested-example: arguments -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="arguments")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "print('two words')")
assert command.argv == ("-c", "print('two words')")
assert command.argv_with_program == (sys.executable, *command.argv)
```

For domain-specific constraints, use the typed builders in `cuprum.builders`
for Git, rsync, and tar. They validate relevant paths, refs, and options at
construction time; see
[Wrap commands in project builders](#wrap-commands-in-project-builders).

Use `ProgramCatalogue.from_project(ProjectSettings(...))` when one project's
name, documentation locations, and noise rules should travel with its commands.
Cuprum stores noise rules for downstream log processing but does not apply them
itself. A catalogue that lists the same program twice raises
`DuplicateProgramError`; a multi-project `ProgramCatalogue(projects=...)` with
two projects of the same name raises `DuplicateProjectError`. Both are
`ValueError` subclasses importable from `cuprum.catalogue`.
[Define an application catalogue](#define-an-application-catalogue) shows a
complete example.

## Control output

### Capture and echo

Capture is on and echo is off by default. `stdout` and `stderr` are decoded
strings when capture is on; they are `None` when it is off. Echo sends output
to parent-facing sinks and can be enabled per stream. Capture and echo are
independent. `RunOutputOptions.on_line` observes decoded lines even when
capture and echo are off. Its callback runs synchronously and should return
promptly; line order is guaranteed within each stream, not across streams.

### Line-level output

<!-- tested-example: output -->

```python
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="output")
python = sh.make(Program(sys.executable), catalogue=catalogue)
lines = []
result = python("-c", "print('one'); print('two')").run_sync(
    output=RunOutputOptions(capture=False, on_line=lines.append),
)
assert result.ok and result.stdout is None
assert [(line.stream, line.text) for line in lines] == [
    ("stdout", "one"),
    ("stdout", "two"),
]
```

For async consumers needing backpressure, `SafeCmd.lines()` returns an async
iterator of `LineEvent(stream, at, text)`; its `result` is available after
iteration finishes. Breaking out of the loop does not stop the child: close the
stream with `async with cmd.lines() as stream:` or `await stream.aclose()` to
guarantee teardown. Pipelines have no `lines()` method; `on_line` on a pipeline
run observes final-stage stdout and every stage's stderr. Interior stdout feeds
the next stage instead.

Echo normally limits each mirrored line to 64 KiB, including its truncation
marker and terminator; captured output remains complete. Set
`max_echo_line_bytes=None` only when unbounded mirroring is appropriate.
`CommandResult.relay_fallbacks` records handled text-sink encoding failures by
stream without including output content. Other sink errors may propagate.

### Quiet children

`RunOutputOptions(idle_after=30.0)` enables an optional heartbeat after 30
seconds without outward output, then once per further silent interval. The
default is off. The built-in notification goes to the configured stderr sink or
`sys.stderr`; it is not captured child output. A pipeline monitors its final
stdout and all stage stderr streams. `on_idle(total, idle)` replaces the
built-in notification. Keep the callback and sink writes prompt: they run on
the command's event loop. A heartbeat reports silence, not a deadlock, and does
not alter the deadline or exit status.

## Supply input, environment, and a deadline

`StdinInput(text=...)` uses the context's encoding. Use `StdinInput(data=...)`
for bytes; specify only one. `ExecutionContext.env` overlays the live parent
environment; an empty mapping still inherits it. `cwd` changes the child's
working directory. A call-level `timeout` overrides `ExecutionContext.timeout`.

<!-- tested-example: input-and-context -->

```python
import sys

from cuprum import Program, ExecutionContext, ProgramCatalogue, StdinInput, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="input")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python(
    "-c", "import os,sys; print(os.getenv('CUPRUM_EXAMPLE'), sys.stdin.read())"
)
result = command.run_sync(
    context=ExecutionContext(env={"CUPRUM_EXAMPLE": "ready"}),
    stdin=StdinInput(text="hello"),
    timeout=5,
)
assert result.stdout == "ready hello\n"
```

A timeout raises `TimeoutExpired`; cancellation of an async run also starts
child teardown. `cancel_grace` in `ExecutionContext` configures the wait
between termination and forced kill. Catch timeout separately from child exit.

## Connect a pipeline

Use `|` between command objects. Only final-stage stdout is captured; each
stage has an exit result. Inspect `PipelineResult.ok` or `failure_index`, since
the final stage can succeed after an earlier failure.

<!-- tested-example: pipeline -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="pipeline")
python = sh.make(Program(sys.executable), catalogue=catalogue)
producer = python("-c", "print('hello')")
consumer = python("-c", "import sys; print(sys.stdin.read().upper(), end='')")
result = (producer | consumer).run_sync()
assert result.ok
assert result.stdout == "HELLO\n"
assert [stage.exit_code for stage in result.stages] == [0, 0]
```

When a non-final stage fails while peers remain active, Cuprum terminates those
peers. The first observed failure is recorded in `failure_index`;
near-simultaneous completions may use stage order as a tie-break. Interior
stdout is consumed by the next stage and appears as `None` in that stage's
result.

## Run commands concurrently

`run_concurrent()` is async; `run_concurrent_sync()` is for synchronous
callers. Set `ConcurrentConfig.concurrency` to bound simultaneous children.
Results follow submission order. `failures` indexes the returned result tuple;
`failure_submission_indices` maps failures to the original commands when
fail-fast cancellation omitted some results.

<!-- tested-example: concurrent -->

```python
import sys

from cuprum import Program, ConcurrentConfig, ProgramCatalogue, run_concurrent_sync, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="concurrent")
python = sh.make(Program(sys.executable), catalogue=catalogue)
commands = [python("-c", f"print({number})") for number in (1, 2)]
result = run_concurrent_sync(*commands, config=ConcurrentConfig(concurrency=2))
assert result.ok
assert [item.stdout for item in result.results] == ["1\n", "2\n"]
assert result.submission_indices == (0, 1)
```

With `fail_fast=True`, remaining commands are cancelled after the first
non-zero exit. A command that already finished can still appear in results. Use
`submission_indices` rather than assuming a compacted result position equals
its original submission index.

## Apply a policy

A catalogue controls builder creation. A scope additionally narrows which
commands may run. Nested scopes cannot widen their parent's allowlist.
Registrations such as `env()`, `before()`, `after()`, and `observe()` are
context-local and are removed when their context manager exits. They do not
change global environment variables.

<!-- tested-example: scoped-policy -->

```python
import sys

from cuprum import Program, ProgramCatalogue, env, scoped, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="policy")
python = sh.make(Program(sys.executable), catalogue=catalogue)
with scoped(catalogue=catalogue), env(CUPRUM_EXAMPLE="scoped"):
    result = python("-c", "import os; print(os.getenv('CUPRUM_EXAMPLE'))").run_sync()
assert result.stdout == "scoped\n"
```

`ScopeConfig` accepts allowlist, hook, timeout, and environment settings when a
catalogue alone is insufficient. `allow()` is the explicit way to widen policy:
it adds programs to the current allowlist, even inside a restricted scope,
until the registration is detached or its block exits. Calling `allow()` in the
default unrestricted context restricts it to exactly the programs allowed.
Review `allow()` calls as policy changes.

A failing `before` or `after` hook is application code and can affect a run; see
[Run code around every command](#run-code-around-every-command). Use
`observe()` for structured `ExecEvent` lifecycle events; telemetry adapters in
`cuprum.adapters` project them to logging, metrics, or tracing. Avoid making
untrusted arguments into metric labels or log fields.

## Observe production runs

For quiet jobs, set `idle_after` as above. For per-line progress, choose
`on_line` or `lines()`. For lower-volume diagnostics, use lifecycle events,
aggregate Python stream-operation observation, or optional Rust-pump events.
These channels report different facts: a heartbeat means no monitored output
was read; it says nothing about whether the child is stuck. A Rust pump decline
means Cuprum used another stream path, not that the child failed. Keep
callbacks bounded and avoid recording payloads as metric labels.

`GitHubActionsSink` from `cuprum.sinks` is opt-in through
`RunOutputOptions(sink=...)`; see
[Present output in GitHub Actions](#present-output-in-github-actions). The
[migration guide](v0-2-0-migration-guide.md) shows adoption forms and optional
stream metrics.

## Migrate a caller

Replace a shell string with a declared executable and separate arguments. Keep
flags positional unless the tool accepts `--name=value`. Replace
`subprocess.run(..., check=True)` with a result check according to the
application's error policy. `ExecutionContext.env` is an overlay, so code that
needs a replacement environment must implement that policy explicitly. The
[0.2.0 migration guide](v0-2-0-migration-guide.md) covers line observation,
result measurements, heartbeats, and presentation sinks.

## Troubleshoot a run

| Symptom                 | Check                                                                                     |
| ----------------------- | ----------------------------------------------------------------------------------------- |
| `UnknownProgramError`   | Register the exact program in the builder's catalogue.                                    |
| `ForbiddenProgramError` | Inspect the active scope and parent allowlist.                                            |
| `FileNotFoundError`     | Confirm the executable path or inherited `PATH`.                                          |
| `TimeoutExpired`        | Set an appropriate deadline; inspect the child for slow or blocked work.                  |
| Non-zero `exit_code`    | Use captured stderr and the tool's documented exit codes.                                 |
| No visible output       | Capture is silent by default; pass `RunOutputOptions(echo=True)` or read `result.stdout`. |
| No heartbeat            | Supply a positive `idle_after`; output activity resets its clock.                         |
| No native speed-up      | The optional extension may be unavailable or this operation may use Python.               |

_Table 1: First checks for common execution outcomes._

For the optional native extension, see
[Choosing a stream backend](#choosing-a-stream-backend) and
[Troubleshooting the native extension](#troubleshooting-the-native-extension).
Benchmarking and the implementation's verification are maintainer topics,
covered in the
[developers' guide](developers-guide.md), [Rust boundary verification](rust-boundary-verification.md),
and the [Cuprum design](cuprum-design.md); none of them is needed to run
commands.

## Going further

These recipes build on the sections above for tasks that come up once an
application relies on Cuprum. Each one is a complete, executed example.

### Define an application catalogue

Give an application's programs a named project so that every command carries
its documentation links and log-noise rules. `ProgramCatalogue.from_project()`
builds a catalogue from one `ProjectSettings`; use
`ProgramCatalogue(projects=(...))` when several projects must each own their
programs. Cuprum stores `noise_rules` for downstream log processing but does
not filter output itself.

<!-- tested-example: application-catalogue -->

```python
import sys

from cuprum import Program, ProgramCatalogue, ProjectSettings, sh
from cuprum.catalogue import DuplicateProgramError

PYTHON = Program(sys.executable)
settings = ProjectSettings(
    name="report-builder",
    programs=(PYTHON,),
    documentation_locations=("docs/reports.md",),
    noise_rules=(r"^progress:",),
)
catalogue = ProgramCatalogue.from_project(settings)

command = sh.make(PYTHON, catalogue=catalogue)("-c", "print('ok')")
assert command.project.name == "report-builder"
assert command.project.noise_rules == (r"^progress:",)
assert catalogue.lookup(PYTHON).project.documentation_locations == ("docs/reports.md",)

try:
    ProgramCatalogue.from_programs(PYTHON, PYTHON)
except DuplicateProgramError as exc:
    assert exc.program == PYTHON
else:
    raise AssertionError("a program listed twice should be rejected")
```

`catalogue.visible_settings` returns a read-only mapping from project name to
`ProjectSettings` for services that propagate this metadata. An absolute
program path is allowlisted exactly as written, so `/usr/bin/git` does not also
permit `git` found on `PATH`.

### Wrap commands in project builders

Centralize argument construction in small functions that validate input and
return a `SafeCmd`. Callers then cannot build an unvalidated command line.
`cuprum.builders` provides ready-made builders for git, rsync, and tar, plus the
`safe_path()` and `git_ref()` validators they use. Those builders use the
default catalogue, so building their commands does not require the tools to be
installed; running them does.

<!-- tested-example: project-builders -->

```python
import sys
from pathlib import Path

from cuprum import Program, ProgramCatalogue, SafeCmd, sh
from cuprum.builders import git_rev_parse, safe_path

PYTHON = Program(sys.executable)
CATALOGUE = ProgramCatalogue.from_programs(PYTHON, name="line-counter")


def count_lines(path: Path) -> SafeCmd:
    """Build a command that counts the lines in one existing file."""
    checked = safe_path(path.resolve())
    script = "import sys; print(sum(1 for _ in open(sys.argv[1])))"
    return sh.make(PYTHON, catalogue=CATALOGUE)("-c", script, str(checked))


result = count_lines(Path("README.md")).run_sync()
assert result.ok and int(result.stdout) > 0

command = git_rev_parse("main")
assert command.argv_with_program == ("git", "rev-parse", "main")
try:
    git_rev_parse("main..evil")
except ValueError:
    pass
else:
    raise AssertionError("an unsafe ref should be rejected")
```

`safe_path()` rejects empty paths, NUL characters, and `..` segments, and
requires an absolute path unless `allow_relative=True` is passed. `git_ref()`
rejects whitespace, a leading `-`, `..`, `@{`, and other constructs that Git
would interpret as options or revision syntax.

### Stream lines as they arrive

Use `lines()` when a caller should react to output before the command finishes.
Use it as an async context manager whenever the loop can exit early: leaving
the block closes the stream, which terminates the child in the same way as a
cancelled `run()`.

<!-- tested-example: stream-lines -->

```python
import asyncio
import sys

from cuprum import Program, ProgramCatalogue, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="follow")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python(
    "-c", "import time\nfor n in range(100):\n print(n, flush=True); time.sleep(0.01)"
)


async def first_line() -> str:
    async with command.lines() as stream:
        async for event in stream:
            # Leaving the block closes the stream and stops the child.
            return event.text
    raise AssertionError("the child should print at least one line")


assert asyncio.run(first_line()) == "0"
```

After a loop that runs to completion, `stream.result` holds the same
`CommandResult` that `run()` would have returned.

### Handle a slow command

Pass `timeout` to `run()` or `run_sync()` and catch `TimeoutExpired`, which
subclasses the built-in `TimeoutError`. `ExecutionContext.cancel_grace` sets
how long Cuprum waits after asking the child to terminate before killing it.
Output captured before the deadline stays available on the exception.

<!-- tested-example: slow-command -->

```python
import sys

from cuprum import ExecutionContext, Program, ProgramCatalogue, TimeoutExpired, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="slow")
python = sh.make(Program(sys.executable), catalogue=catalogue)
slow = python("-c", "import time; time.sleep(30)")

try:
    slow.run_sync(timeout=0.5, context=ExecutionContext(cancel_grace=0.2))
except TimeoutExpired as exc:
    assert exc.timeout == 0.5
    assert exc.output == "" and exc.stderr == ""
else:
    raise AssertionError("the command should have timed out")
```

[Timeouts](#timeouts) in the reference covers scope-level defaults, pipeline
deadlines, and the log records each expiry writes.

### Restrict a block of code to specific programs

A scope narrows which catalogued programs may run inside a `with` block.
Running anything else raises `ForbiddenProgramError`, a `PermissionError`
subclass, before a process starts. `current_context().is_allowed()` checks a
program without running it.

<!-- tested-example: restricted-scope -->

```python
import sys

from cuprum import (
    ForbiddenProgramError,
    Program,
    ProgramCatalogue,
    ScopeConfig,
    current_context,
    scoped,
    sh,
)

PYTHON = Program(sys.executable)
OTHER = Program("some-other-tool")
catalogue = ProgramCatalogue.from_programs(PYTHON, OTHER, name="policy")
python = sh.make(PYTHON, catalogue=catalogue)

with scoped(ScopeConfig(allowlist=frozenset([OTHER]))):
    assert not current_context().is_allowed(PYTHON)
    try:
        python("-c", "pass").run_sync()
    except ForbiddenProgramError:
        pass
    else:
        raise AssertionError("the scope should forbid the interpreter")
```

### Run code around every command

`before(hook)` receives each command before it starts, and `after(hook)`
receives the command and its `CommandResult` once it finishes. Before hooks run
in registration order and after hooks in reverse order, so an outer scope's
setup runs first and its cleanup last. `logging_hook()` pairs the two to write
start and exit records through the standard `logging` module.

<!-- tested-example: before-and-after-hooks -->

```python
import logging
import sys

from cuprum import Program, ProgramCatalogue, after, before, logging_hook, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="hooks")
python = sh.make(Program(sys.executable), catalogue=catalogue)
seen = []


def note_start(cmd):
    seen.append(("before", cmd.argv[-1]))


def note_finish(cmd, result):
    seen.append(("after", result.exit_code))


logger = logging.getLogger("myapp.commands")
with before(note_start), after(note_finish), logging_hook(logger=logger):
    python("-c", "pass").run_sync()
assert seen == [("before", "pass"), ("after", 0)]
```

An exception from a before or after hook propagates to the caller, so keep
hooks that must never affect a run defensive.

### Log, measure, and trace runs

The adapters in `cuprum.adapters` turn lifecycle events into structured log
records, metrics, and trace spans. Each is an observe hook, so register it with
`sh.observe()`. `InMemoryMetrics` and `InMemoryTracer` are reference backends;
production code implements the `MetricsCollector` protocol, or the `Tracer` and
`Span` protocols, over its telemetry library.

<!-- tested-example: telemetry-adapters -->

```python
import logging
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.adapters.logging_adapter import structured_logging_hook
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook

catalogue = ProgramCatalogue.from_programs(sys.executable, name="telemetry")
python = sh.make(Program(sys.executable), catalogue=catalogue)
metrics = InMemoryMetrics()
tracer = InMemoryTracer()
logger = logging.getLogger("myapp.telemetry")

with (
    sh.observe(structured_logging_hook(logger=logger)),
    sh.observe(MetricsHook(metrics)),
    sh.observe(TracingHook(tracer)),
):
    python("-c", "print('traced')").run_sync()

assert metrics.counters["cuprum_executions_total"] == 1
assert [span.name for span in tracer.spans] and tracer.spans[0].ended
```

The structured logging adapter records `argv` verbatim, so a secret passed as a
command-line argument reaches the log. Pass secrets through the environment or
a file instead. [Metrics adapter](#metrics-adapter) and
[Tracing adapter](#tracing-adapter) list every metric and span attribute.

### Present output in GitHub Actions

`GitHubActionsSink` from `cuprum.sinks` groups a run's echoed output in a
collapsible GitHub Actions log section and turns a failure into an error
annotation. It activates only when `GITHUB_ACTIONS` is `true`; pass
`force=True` to reproduce the framing elsewhere. The sink changes only what is
echoed, never capture or the result.

<!-- tested-example: github-actions-sink -->

```python
import io
import sys

from cuprum import Program, ProgramCatalogue, RunOutputOptions, sh
from cuprum.sinks import GitHubActionsSink

catalogue = ProgramCatalogue.from_programs(sys.executable, name="ci")
python = sh.make(Program(sys.executable), catalogue=catalogue)
workflow_log = io.StringIO()
sink = GitHubActionsSink(workflow_log, title="Build", force=True)
result = python("-c", "print('building')").run_sync(
    output=RunOutputOptions(echo=True, sink=sink),
)
assert result.ok and result.stdout == "building\n"
assert workflow_log.getvalue().startswith("::group::Build")
```

## Operational reference

These contracts support diagnosis and integration after the first run. They
retain the exact event and configuration names used by telemetry.

### Timeouts

Use the `timeout` parameter on `run()` / `run_sync()` to enforce a wall-clock
limit in seconds. Timeouts are opt-in; when left as `None` no limit is
enforced. When a timeout expires, Cuprum terminates the subprocess, waits for
`cancel_grace`, escalates to `SIGKILL` if needed, and raises `TimeoutExpired`
(mirroring `subprocess.TimeoutExpired`).

Any output already captured before the timeout fired is preserved on the
exception: `exc.output` / `exc.stderr` hold the partial stdout/stderr (or
`None` when `capture=False`), so callers can inspect what the command produced
before it was killed.

Under `capture=True` those attributes are always strings, never `None`: a
stream that produced nothing before the deadline reports the empty string.

Once the process has died, Cuprum gives its readers a brief, fixed window to
observe end of file (EOF). A reader that is still waiting when the window
closes is cancelled, but keeps whatever it had already read. This happens when
a grandchild process inherited the pipe and holds it open; the window is fixed
because teardown must never wait on a pipe that may never close.

#### Where a timeout comes from

Cuprum uses the first of these that is set:

1. An explicit `timeout` argument on `run()` / `run_sync()`.
2. `ExecutionContext.timeout`.
3. A `ScopeConfig(timeout=...)` default from the enclosing `scoped()` block.

`ScopeConfig(timeout=...)` and `CuprumContext(timeout=...)` validate their
timeouts when constructed. Values must be finite and non-negative; `None` means
no timeout, and numeric values are stored as `float`. Negative values, `NaN`,
and positive or negative infinity raise `ValueError`, as does a value too large
to convert to `float`.

A scope-level default applies to every command run inside it. The expired
command's partial output is still available on the exception:

<!-- tested-example: scope-timeout -->

```python
import sys

from cuprum import Program, ProgramCatalogue, ScopeConfig, TimeoutExpired, scoped, sh

catalogue = ProgramCatalogue.from_programs(sys.executable, name="timeouts")
python = sh.make(Program(sys.executable), catalogue=catalogue)
command = python("-c", "import time; print('started', flush=True); time.sleep(30)")

with scoped(ScopeConfig(timeout=1.0)):
    try:
        command.run_sync()
    except TimeoutExpired as exc:
        assert exc.timeout == 1.0
        # A slow start can expire before the child prints anything.
        assert exc.output in {"", "started\n"}
    else:
        raise AssertionError("the scope timeout should have expired")
```

#### Non-positive timeouts

A `timeout` of `0` or a negative value is treated as an already-elapsed
deadline, so the command expires immediately, without waiting for the process
to exit on its own. Cuprum terminates the running process (or every pipeline
stage), waits for it to exit — honouring `cancel_grace` and escalating to
`SIGKILL` if needed — drains the stream consumers, and then raises
`TimeoutExpired`.

#### Pipeline timeouts

A pipeline enforces its deadline once for the whole run; partial output follows
the same capture rules as a successful run. Expiry is reported per stage: each
stage gets a `timeout` event, a `cuprum.timeout` record with its `pid`, and one
`cuprum_timeouts_total` increment. The fields and `timeout_mode` values match
the single-command case.

#### Timeout diagnostics

Every expiry writes a structured `WARNING` record to the `cuprum.timeout`
logger, even when no observe hook is registered. The
`subprocess_timeout_expired pid=… timeout=… mode=…` record carries:

- `cuprum_operation`: `"wait"`
- `cuprum_pid`: the subprocess process ID (PID), or `None` when no process was
  spawned
- `cuprum_timeout_s`: the configured timeout in seconds
- `cuprum_timeout_mode`: `"elapsed_deadline"` or `"non_positive_immediate"`
- `cuprum_error_type`: `"TimeoutError"`

If cleanup fails to drain a stream consumer, an `ERROR`
`subprocess_teardown_drain_failed pid=… errors=…` record carries
`cuprum_operation` (`"drain"`), `cuprum_teardown_outcome` (`"drain_error"`),
`cuprum_pid`, and `cuprum_error_type` with the comma-joined failure classes.

The drain failure is absorbed so it cannot displace `TimeoutExpired` or
`CancelledError`. Logging is best-effort; the same values are emitted as
`timeout` and `teardown_error` events, so the two channels cannot disagree.

### Structured execution events

For richer observability, register an observe hook with `sh.observe()`. Observe
hooks receive `ExecEvent` values describing:

- `plan` — intent to execute the program (argv/cwd/env resolved).

- `start` — subprocess spawned (pid available).

- `stdout` / `stderr` — decoded output emitted as lines.

- `exit` — subprocess finished (exit code and duration).

- `stdin` — input supplied through `StdinInput` was written to the child;
  `byte_count` gives its size.

- `stdin_error` — writing or closing the child's stdin failed, typically
  because the child stopped reading early, as `head` does. `operation` names
  the failing step (`write` or `close`), and execution continues.

- `timeout` — the run exceeded its deadline (ancillary; emitted before the
  preserved `exit` event and the public `TimeoutExpired`).

- `teardown_error` — a stream consumer drained with an unexpected error during
  cleanup (ancillary; the error is absorbed to preserve the primary exception).

- `capture_eof_grace_expired` — a capturing drain exhausted its fixed EOF grace
  budget while one or more readers remained pending (ancillary).

- `pipeline_fail_fast` — a pipeline is being torn down early because a
  non-final stage was the first to fail. Emitted at most once per pipeline,
  before every other still-running stage — upstream producers and downstream
  consumers alike — is terminated, and carrying `stage_index`, `stage_count`,
  `exit_code`, and `duration_s` for the failing stage. It is a decision rather
  than a lifecycle phase: the failing stage's own `exit` event still follows,
  and no stage's events are skipped or reordered because of it. Read
  `stage_index` and `stage_count` from these typed fields rather than from the
  event's tags: `ExecutionContext.tags` are merged last, so a caller that sets
  its own `pipeline_stage_index` or `pipeline_stages` tag shadows the tag
  copies, whereas the typed fields always report the stage the pipeline
  actually acted on.

Line events omit their line terminators. A carriage-return and line-feed (CRLF)
pair split across internal reads is held until both bytes arrive, so it emits
one line event and never an empty event for the boundary.

Hooks can be used for structured logging, metrics, or tracing without coupling
Cuprum to a specific telemetry library.

The `timeout` event carries `operation` (`"wait"`), `pid`, `timeout_s`,
`error_type` (`"TimeoutError"`), and `timeout_mode`: `"elapsed_deadline"` for a
positive elapsed deadline or `"non_positive_immediate"` for a non-positive
deadline that expires without awaiting the process. The `teardown_error` event
carries `operation` (`"drain"`), `pid`, and `error_type`, the comma-joined
classes raised while cancelling and draining stream consumers. It fires at most
once per execution and excludes expected `CancelledError` failures.

The `capture_eof_grace_expired` event carries the execution's `exec_id` and
`pid`, `operation` (`"drain"`), `eof_grace_s`, and `pending_readers` (one or
two). It is emitted only when a capturing drain reaches the fixed grace limit;
the corresponding `cuprum_capture_eof_grace_expired_total` metric uses only the
`program` and `project` labels, and tracing adds a
`cuprum.capture_eof_grace_expired` event to the matching span. The grace-expiry
event itself carries no captured stdout or stderr payload.

These ancillary events preserve the public outcome: `start` and `exit` are
unchanged, and synchronous hook failures handling `timeout`, `teardown_error`,
or `capture_eof_grace_expired` are suppressed rather than masking
`TimeoutExpired` or `CancelledError`.

Each event carries a stable `exec_id` correlation token: every lifecycle event
for one execution — a single command, or one stage of a pipeline — shares the
same token. Hooks that track per-execution state should correlate by `exec_id`
rather than `pid`, which the operating system can recycle across executions.
Events with `exec_id=None` cannot be correlated, so correlation-consuming hooks
(such as the tracing adapter) drop them.

Awaitable hook results are scheduled as `asyncio.Task` instances and awaited
before the run completes.

#### Aggregate Python stream-operation events

For opt-in aggregate telemetry from the pure-Python stream paths, register a
hook with `cuprum.stream_observation.observe_stream_operation`:

<!-- tested-example: stream-operation-metrics -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics
from cuprum.adapters.stream_metrics import stream_operation_metrics_hook
from cuprum.stream_observation import observe_stream_operation

catalogue = ProgramCatalogue.from_programs(sys.executable, name="stream-metrics")
python = sh.make(Program(sys.executable), catalogue=catalogue)
metrics = InMemoryMetrics()
with observe_stream_operation(stream_operation_metrics_hook(metrics)):
    python("-c", "print('hello')").run_sync()
assert metrics.counters["cuprum_stream_operation_bytes_total"] > 0
```

One `StreamOperationEvent` is emitted when each completed stream drain or
pipeline transfer finishes. It reports aggregate `bytes_consumed`, completed
`read_operations` (including EOF), and monotonic `duration_s`. Its closed
`operation` values are `stream_drain` and `pipeline_transfer`; its closed
`outcome` values are `eof`, `cancelled`, `failed`, `downstream_closed`, and
`post_close_drain_timeout`. No event is emitted per read or per chunk, and no
payload is included.

Observer failures are best-effort and do not alter stream execution. `exec_id`
is present only when an existing pipeline-stage correlation context safely
provides it; direct drains carry `None`.

The optional `stream_operation_metrics_hook` records these metrics, all in the
units named by their metric:

- `cuprum_stream_operation_bytes_total` (bytes counter)
- `cuprum_stream_operation_read_operations_total` (read-operation counter)
- `cuprum_stream_operation_duration_seconds` (seconds histogram)

Metrics use only the bounded `operation` and `outcome` labels. They never label
payload, read size, path, PID, descriptor, command argument, exception text, or
another unbounded value.

#### When an observe hook raises

A failing observe hook fails the run. Cuprum logs the failure and then
re-raises the hook's _own_ exception type out of `run()` / `run_sync()`; it is
never swallowed. The two hook kinds differ only in when that happens:

- A **synchronous** hook raises inline, at the moment the event is emitted.
  Emission of that event stops there, so hooks registered after it do not
  receive that event, and the exception surfaces immediately — before the
  subprocess is spawned, if the hook failed on `plan`. The raised exception is
  the hook's own; Cuprum's internal wrapper appears only as its `__cause__`.
- An **awaitable** hook raises inside its scheduled task. Every hook still
  receives the event, and the exception surfaces when Cuprum awaits the pending
  tasks before the run returns. When several awaitable hooks fail, only the
  first is raised.

This matters most for hooks that match exhaustively on `ExecEvent.phase` and
reject unknown values. `pipeline_fail_fast` arrived in 0.2.0, so such a hook
written earlier raises on it — and so fails the pipeline — until it grows an
arm for it. A hook that must never influence the run should catch its own
exceptions.

`ExecHook` is defined in `cuprum.events` and re-exported from `cuprum`. Import
it with `from cuprum import ExecHook` or `from cuprum.events import ExecHook`.
The former `from cuprum.context import ExecHook` path is no longer supported;
update that import without changing the hook implementation.

`ExecutionContext.tags` is merged into each event's `tags` mapping. Cuprum also
adds default tags such as the project name and pipeline stage metadata.

### Metrics adapter

The `metrics_adapter` module provides a Prometheus-style metrics hook that
collects counters and histograms. It uses a protocol class so the backend can
be implemented with any preferred metrics library.

The hook collects:

- `cuprum_executions_total`: Counter incremented on each command start
- `cuprum_failures_total`: Counter incremented on non-zero exit
- `cuprum_duration_seconds`: Histogram of execution durations
- `cuprum_stdout_lines_total`: Counter of stdout lines emitted
- `cuprum_stderr_lines_total`: Counter of stderr lines emitted
- `cuprum_stdin_bytes_total`: Counter of successful stdin bytes written
- `cuprum_stdin_errors_total`: Counter of stdin writer failures
- `cuprum_timeouts_total`: Counter of subprocess timeout expiries
- `cuprum_teardown_errors_total`: Counter of stream-consumer drain failures
  during cleanup
- `cuprum_capture_eof_grace_expired_total`: Counter of capturing drains that
  reach the fixed EOF-grace limit with readers still pending
- `cuprum_pipeline_fail_fast_total`: Counter incremented once per pipeline torn
  down early because a non-final stage was the first to fail
- `cuprum_resource_usage_measurements_total`: Counter incremented once per
  terminal `exit` event that recorded a `resource_usage_mode`, including
  `unavailable`
- `cuprum_child_max_rss_bytes`: Histogram of the child's maximum resident set
  size (RSS) in bytes, recorded only when the operating system's `wait4` call
  attributes it to that child
- `cuprum_child_user_cpu_seconds`: Histogram of child user CPU seconds,
  observed only where that figure was actually measured
- `cuprum_child_system_cpu_seconds`: Histogram of child system CPU seconds,
  observed only where that figure was actually measured

All metrics carry `program` and `project` labels; missing, empty, or explicit
`None` project tags fall back to `unknown`.

The four resource metrics also carry a low-cardinality `resource_usage_mode`
label naming how the measurement was obtained: `wait4_child`,
`aggregate_cpu_delta`, or `unavailable`. The label applies only to those four
because it means nothing for any other metric. The three resource histograms
are observed only where a figure was actually measured, while the counter is
emitted for every mode, including `unavailable`. A platform that measures
nothing therefore stays countable, and distinguishable from a run whose samples
went missing.

`cuprum_timeouts_total` counts both modes; use the `timeout` event's
`timeout_mode`, or the `cuprum.timeout` record, to distinguish an elapsed
deadline from an immediate non-positive expiry.

`cuprum_pipeline_fail_fast_total` does **not** use `exec_id`, stage index, exit
code, command arguments, or paths as labels. `exec_id` is unique per execution
and would give the series unbounded cardinality; the others would multiply
series for no aggregate a dashboard needs. Those fields remain on the
`pipeline_fail_fast` event and on the trace span, where per-incident detail
belongs.

A counter spike therefore cannot be joined to a span through the metric itself.
Use `ExecEvent.exec_id` on the observe event, or `cuprum_exec_id` on the
matching structured log record: both carry the same execution token, which
identifies the stage spans behind the spike.

To integrate with a metrics library, implement the `MetricsCollector` protocol.

### Tracing adapter

The `tracing_adapter` module provides an OpenTelemetry-style tracing hook that
creates spans for command execution. It defines protocol classes, so any
tracing library can back it.

The hook creates spans with these attributes:

- `cuprum.program`: The program being executed
- `cuprum.argv`: Full argument vector
- `cuprum.pid`: Process ID
- `cuprum.cwd`: Working directory (when set)
- `cuprum.exit_code`: Exit code (set on span end)
- `cuprum.duration_s`: Duration in seconds (set on span end)
- `cuprum.project`: Project name from tags
- `cuprum.pipeline_stage_index`: Pipeline stage index (if applicable)
- `cuprum.pipeline_stages`: Total pipeline stages (when applicable)
- `cuprum.max_rss_bytes`, `cuprum.user_cpu_seconds`,
  `cuprum.system_cpu_seconds`, `cuprum.resource_usage_mode`: Terminal child
  resource figures and the mode naming their source, set on span end. The mode
  is carried on every terminal `exit` event — `wait4_child`,
  `aggregate_cpu_delta`, or `unavailable` — while the three figures alone are
  absent rather than null when no measurement applies

Output lines (stdout/stderr) are recorded as span events when
`record_output=True` (the default).

#### Ancillary span events

The `stdin_error`, `timeout`, `teardown_error`, and `capture_eof_grace_expired`
phases are recorded as `cuprum.<phase>` events on the execution's open span.
They leave the span neither ended nor marked; a later `exit` closes it normally.
`timeout` is always followed by `exit`, while `teardown_error` may be the
final event.

Ancillary events carry whichever of the `line`, `operation`, `error_type`,
`note`, `timeout_s`, `timeout_mode`, `eof_grace_s`, and `pending_readers`
fields are set. An event without a matching open span is dropped. The
grace-expiry event carries no captured stdout or stderr payload.

#### Correlating events with spans

The hook matches every event to its span by `ExecEvent.exec_id`, a stable token
minted once per execution, or once per stage in a pipeline. It does not use
`pid`, because the operating system can reuse a process ID for a later
execution; `pid` is still recorded as the `cuprum.pid` attribute.

Events emitted by Cuprum always carry an `exec_id`, so ordinary usage is
unaffected. The hook ignores hand-built or legacy events that omit it:

- a `start` event without an `exec_id` creates no span;
- any other event without one is dropped.

A pipeline's `pipeline_fail_fast` event is recorded as a
`cuprum.pipeline_fail_fast` span event on the failing stage's already-open
span, carrying `stage_index`, `stage_count`, `exit_code`, and `duration_s`. It
is `cuprum_exec_id` that joins the record, the event, and that span: the
decision reuses the failing stage's existing execution token rather than
minting one of its own, so the teardown appears in the trace of the stage that
caused it. No separate span is started, and the stage's own `exit` event still
closes and marks the span.

OpenTelemetry integrations implement the `Tracer` and `Span` protocols.

### Choosing a stream backend

Most applications should leave the backend on `auto`. The
`CUPRUM_STREAM_BACKEND` environment variable accepts three values:

- `auto` (default): uses the Rust pathway when the native extension is
  installed and falls back to pure Python otherwise. The choice is recorded
  once as a `DEBUG` `resolved stream backend` record on the `cuprum._backend`
  logger, whose `resolved_backend` field names the pathway.
- `python`: forces the pure Python pathway, for example when debugging or
  reproducing an issue from a pure Python installation.
- `rust`: requires the native extension. Pipeline execution raises
  `ImportError` if it is unavailable, rather than silently falling back.

Set `CUPRUM_STREAM_BACKEND` before first backend resolution in the process.
Cuprum resolves the backend once and caches it, so later changes have no effect.

The backend applies only to inter-stage pipeline pumping. The Rust pathway runs
outside the global interpreter lock (GIL) on a dedicated worker pool that
Cuprum owns, independent of the event loop's default executor.

For stdout/stderr capture, Cuprum always uses the Python pathway, so line
callbacks, echo, and custom encodings behave identically on either backend.

Even with `rust` selected, an individual hop falls back to the Python pump when
its descriptors cannot be borrowed safely; on Windows every asyncio
subprocess-pipe hop does so. Fall-backs never change pipeline output. See
[Why a hop fell back to Python](#why-a-hop-fell-back-to-python) to detect them.

Rust acceleration pays off for large, multi-stage pipelines, especially on
Linux, where the pump uses the zero-copy `splice()` system call between pipes.
macOS uses a read and write loop. For small outputs the difference is usually
negligible, because the saving is per chunk and small payloads have few chunks.
Measure a representative workload before standardizing on `rust`: from a source
checkout, `make benchmark-e2e` runs the end-to-end throughput suite, and the
[developers' guide](developers-guide.md#running-the-benchmark-suite) explains
its scenarios.

### Checking the native extension

`is_rust_available()` reports whether the native backend can be used. It returns
`False` on a pure Python installation rather than raising, while other import
failures still surface so that a broken installation is visible:

<!-- tested-example: rust-availability -->

```python
import cuprum

assert isinstance(cuprum.is_rust_available(), bool)
```

The same check runs from a shell:

<!-- shell-example: rust-availability -->

```shell
python -c "import cuprum; print(cuprum.is_rust_available())"
```

### Rust-pump executor-hop spans

Rust-backed pipelines can expose the executor hop that moves bytes between
stages as an opt-in span. Set `CUPRUM_STREAM_BACKEND=rust` before backend
resolution, then register a `Tracer` with `observe_pump_span` in the context
where the pipeline runs. The registration is context-local and can be used as a
context manager.

One span is opened for each registered tracer and each Rust-pump hop that is
actually scheduled. A fast-path decline creates no hop span. The span starts
immediately before executor scheduling and ends from the completion callback,
after the native worker has settled; cancellation therefore includes the
cleanup drain. The pump's own Rust-side span is not parented to it, because
trace context does not cross PyO3, the layer that binds the Rust extension to
Python.

Hop spans contain only bounded attributes: `cuprum.operation` (currently
`rust_pump`), `cuprum.buffer_size`, and `cuprum.outcome`, whose values are
`succeeded`, `failed`, `cancelled`, or `failed_after_cancel`. Successful hops
also include `cuprum.total_bytes` and receive status `OK`; other outcomes do
not receive a success status. The existing `PumpEvent` channel and
`observe_pump` registrations are unchanged. Ordinary tracer observer failures
are contained so they do not alter pump execution, while control-flow
exceptions continue to propagate.

### Why a hop fell back to Python

Selecting the `rust` backend does not guarantee that every inter-stage hop
takes it. Handing the raw pipe descriptors to the Rust pump can fail in ways
that are not errors: the hop runs on the Python pump instead and produces the
same result, more slowly. Nothing surfaces to the caller, so a deployment that
has quietly stopped taking the fast path looks exactly like one that never had
it.

Each fall-back is recorded at `DEBUG` on the `cuprum._pipeline_streams` logger
with a `cuprum_action` of `rust_pump_declined` and one of these `cuprum_reason`
values:

| `cuprum_reason`             | Meaning                                                                                        |
| --------------------------- | ---------------------------------------------------------------------------------------------- |
| `raw_fd_unavailable`        | at least one asyncio transport exposed no raw descriptor, so there was nothing to hand over    |
| `reader_unresumable`        | the reader transport exposes `pause_reading` but not `resume_reading`, so it was left unpaused |
| `reader_pause_failed`       | the reader transport could not be paused, so asyncio might still consume the descriptor        |
| `blocking_mode_unavailable` | the descriptors could not be switched to the blocking mode the pump requires                   |
| `duplicate_fds_unavailable` | a transport descriptor closed before the worker's copy of it could be made                     |
| `platform_unsupported`      | Windows Proactor pipes use overlapped handles that synchronous Rust I/O cannot safely use      |

_Table 2: Reasons an inter-stage hop declines the Rust pump._

These records sit at `DEBUG` rather than `WARNING` because a fall-back is a
routing decision, not a fault; on platforms where the fast path does not apply,
every hop would otherwise warn. Raise that one logger when investigating
throughput:

<!-- tested-example: decline-logging -->

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("cuprum._pipeline_streams").setLevel(logging.DEBUG)
assert logging.getLogger("cuprum._pipeline_streams").isEnabledFor(logging.DEBUG)
```

`raw_fd_unavailable` on every hop usually means the streams are not real
operating-system pipes. The other reasons mean the descriptors were found but
could not be borrowed safely, which is worth investigating rather than
accepting. On Windows, every asyncio subprocess-pipe hop records
`platform_unsupported` and uses the Python pump.

### A pump failure hidden by cancellation

Cancelling a pipeline while the Rust pump is mid-transfer reports the
cancellation to the caller, not any failure inside the pump. Such a failure is
recorded on the same logger with a `cuprum_action` of
`rust_pump_failed_after_cancel` and the original traceback attached, so it
stays diagnosable. It also sits at `DEBUG`, so the logger adjustment above
reveals it.

### A teardown step that failed and was ignored

Returning descriptors after a native hop is best-effort: an error while closing
a worker-owned duplicate, restoring blocking mode, or resuming the reader is
suppressed rather than raised, because the transfer itself has already settled.
Each suppression records a `DEBUG` event with a `cuprum_action` of
`rust_pump_teardown_failed`, a `cuprum_site` naming the step, and the exception
class and `errno`.

The `resume`, `reader_close`, and `restore_blocking` sites log on
`cuprum._pipeline_stream_fds`; the `writer_close`, `resume_reader`, and
`restore_state` sites log on `cuprum._pipeline_streams`. The record carries
nothing drawn from the transferred data. Any of these records indicates a
teardown problem worth investigating.

### A pump hand-off failed before submission

When Cuprum cannot prepare the worker-owned descriptors, it rolls the hand-off
back and the hop falls back to the Python pump. A writer that could not be
duplicated records `duplicate_writer_failed`, and descriptors that could not be
switched to blocking mode record `blocking_setup_failed`. The
`cuprum._pipeline_streams` logger records a bounded `DEBUG` diagnostic with
`cuprum_action="rust_pump_handoff_failed"`, the exception class, and `errno`
when available.

Two setup failures still reach the caller. A rejected executor submission
reports `executor_submission_rejected` and re-raises after rollback, because
every later hop would be rejected the same way. A failure to duplicate
descriptors that Cuprum already owns means descriptor exhaustion, which a
fall-back cannot route around. Both close the writer transport first, so the
downstream stage exits and the failure reaches the caller promptly instead of
waiting for a deadline.

### Counting pump routing decisions

Aggregating debug logs answers "why did this hop fall back?" one record at a
time. To count the same decisions instead, register a pump observer. It is a
separate channel from `sh.observe`: pump events describe an internal routing
decision rather than a command's lifecycle, so they are not `ExecEvent` values
and never reach an observe hook. An `ExecEvent` consumer registered elsewhere
in the process is unaffected.

The channel counts declines, Rust writer-resource hand-off outcomes,
post-cancellation failures, and native-pump cleanup. A successful hand-off
emits a `handoff` event and increments
`cuprum_rust_pump_handoff_total{outcome="submitted"}` once. The same counter
records each failed hand-off outcome once, so it can show both the number of
hops that were submitted successfully and each terminal hand-off failure.

#### Cleanup after cancellation

When a pipeline is cancelled mid-hop, the native worker keeps ownership of its
descriptors until it settles, and the caller waits for that cleanup. The pump
channel reports each step:

- `cleanup_started` when the caller begins waiting for the native worker;
- `cleanup_completed` when the worker releases descriptor ownership, with the
  monotonic wait in `PumpEvent.duration_s`;
- `cleanup_grace_expired` when the caller stops waiting, with the time spent in
  `PumpEvent.elapsed_s`;
- `cleanup_deferred` when the worker later settles and its completion callback
  finishes the cleanup.

`ExecutionContext(native_pump_cleanup_grace=seconds)` bounds the caller's wait;
the default is 0.5 seconds. When the grace expires, the caller receives its
original `CancelledError` straight away. The worker's descriptors stay
quarantined until its completion callback closes them, and the paused reader
transport is closed at expiry, while its event loop can still run the close.
The native worker pool is independent of `asyncio.run()` shutdown, so the same
bound holds for `run_sync()`, and a callback that completes after the loop has
closed still releases its descriptors.

To place these steps in a trace, register the same `TracingHook` with both
`sh.observe(hook)` and `observe_pump(hook.record_pump_event)`. The hook adds
`cuprum.cleanup_started`, `cuprum.cleanup_completed`,
`cuprum.cleanup_grace_expired`, and `cuprum.cleanup_deferred` events to the
source stage's open span, found through its `ExecId`. Each event carries the
bounded attributes `operation="native_pump_cleanup"` and `outcome`, plus
`duration_s` or `elapsed_s` in monotonic seconds where they apply.

The token is not added as a trace attribute, and no PID, descriptor number,
command argument, or exception text is emitted. An event without a matching
open span is dropped; cleanup tracing neither changes span status nor ends the
span.

#### Cleanup DEBUG records

Cancellation cleanup also emits `DEBUG` records on the
`cuprum._pipeline_streams` logger. Each record has
`cuprum_action="rust_pump_cleanup"` and
`cuprum_operation="native_pump_cleanup"`, and a `cuprum_outcome` naming the
step:

- `cuprum_outcome="started"` when the wait begins;
- `cuprum_outcome="completed"`, with `cuprum_duration_s`, on normal
  completion;
- `cuprum_outcome="grace_expired"`, with `cuprum_elapsed_s`, when the caller
  stops waiting;
- `cuprum_outcome="deferred"` from the eventual completion callback, emitted
  only after the native worker has released descriptor ownership.

#### Pump metrics

`PumpMetricsHook` takes the same `MetricsCollector` protocol as `MetricsHook`,
so one collector can back both channels:

<!-- tested-example: pump-metrics -->

```python
import sys

from cuprum import Program, ProgramCatalogue, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.adapters.pump_metrics import PumpMetricsHook
from cuprum.pump_observation import observe_pump

catalogue = ProgramCatalogue.from_programs(sys.executable, name="pump-metrics")
python = sh.make(Program(sys.executable), catalogue=catalogue)
pipeline = python("-c", "print('hello')") | python(
    "-c", "import sys; print(sys.stdin.read(), end='')"
)
metrics = InMemoryMetrics()
with sh.observe(MetricsHook(metrics)), observe_pump(PumpMetricsHook(metrics)):
    result = pipeline.run_sync()
assert result.ok
assert metrics.counters["cuprum_executions_total"] == 2
# Pump counters appear only when a hop reaches the Rust pump decision.
pump_counters = [name for name in metrics.counters if "_rust_pump_" in name]
assert all(name.startswith("cuprum_rust_pump_") for name in pump_counters)
```

| Metric                                         | Labels    | Incremented when                                                 |
| ---------------------------------------------- | --------- | ---------------------------------------------------------------- |
| `cuprum_rust_pump_declined_total`              | `reason`  | a hop fell back from the Rust pump to the Python pump            |
| `cuprum_rust_pump_failed_after_cancel_total`   | none      | a cancelled hop's Rust worker failure was consumed and recorded  |
| `cuprum_rust_pump_cleanup_total`               | none      | native cleanup completed after cancellation                      |
| `cuprum_rust_pump_cleanup_duration_seconds`    | none      | one monotonic duration was observed for completed native cleanup |
| `cuprum_rust_pump_cleanup_grace_expired_total` | none      | caller-facing cleanup grace expired                              |
| `cuprum_rust_pump_cleanup_deferred_total`      | none      | deferred callback cleanup completed                              |
| `cuprum_rust_pump_handoff_total`               | `outcome` | a Rust writer-resource hand-off outcome was reached              |

_Table 3: Metrics emitted by `PumpMetricsHook`._

The cleanup metrics are emitted only when callers register
`observe_pump(PumpMetricsHook(metrics))`. `cuprum_rust_pump_cleanup_total` is
incremented once for every completed native cleanup, and
`cuprum_rust_pump_cleanup_duration_seconds` records one duration observation
for each normal cleanup. The grace-expiry and deferred-cleanup counters each
increment once for their respective outcome. All cleanup metrics are
unlabelled. The `reason` label on the decline counter retains the bounded
cardinality described below.

The `outcome` label on `cuprum_rust_pump_handoff_total` is closed to exactly
`submitted`, `blocking_setup_failed`, `executor_submission_rejected`,
`native_load_failed`, `buffer_validation_failed`,
`platform_writer_transfer_failed`, `native_io_failed`,
`duplicate_writer_failed`, and `reader_preparation_failed`. `outcome` is the
only label on this counter. Descriptor numbers, Windows handle values, errno
values, exception types, exception messages, and tracebacks never become metric
labels.

The `reason` label takes exactly the values in Table 2, plus `unknown`, and
nothing else. Table 2's values are published as the `RustPumpDeclineReason`
enum and `unknown` as `cuprum.adapters.pump_metrics.UNKNOWN_DECLINE_REASON`, so
a dashboard can enumerate the series it will see:

<!-- tested-example: decline-reason-series -->

```python
from cuprum import RustPumpDeclineReason
from cuprum.adapters.pump_metrics import UNKNOWN_DECLINE_REASON

series = [reason.value for reason in RustPumpDeclineReason] + [UNKNOWN_DECLINE_REASON]
assert "platform_unsupported" in series and series[-1] == "unknown"
```

`unknown` is a guard rather than an outcome: it is what a decline whose reason
is not an enum member would be labelled, and no call site produces one. It is
listed because a dashboard filtering on the enum alone would drop such a
decline silently rather than showing it.

`PumpEvent` is a public dataclass and performs no run-time validation, so a
caller can construct one carrying any object as its `reason`. The hook
therefore checks the value against `RustPumpDeclineReason` before labelling and
degrades anything else to `unknown`; a hand-built event cannot widen the label
domain.

Nothing derived from a descriptor, an argument vector, or an exception reaches
a label, so the series count is fixed. A successful hand-off increments the
handoff counter once with `outcome="submitted"`. The `DEBUG` records above are
unchanged — the counters supplement them rather than replacing them.

> [!NOTE]
> A pump hook that raises is reported on the `cuprum.pump_observation` logger
> at `WARNING` with its traceback, and the hop continues. This differs from
> `sh.observe` hooks, whose exceptions fail the command being observed. The
> difference is deliberate: a misconfigured metrics backend must not be able to
> abort a pipe hop that would otherwise have succeeded.
>
> Only `Exception` instances are suppressed. `SystemExit`, `KeyboardInterrupt`,
> and `asyncio.CancelledError` propagate unchanged, because some emission sites
> run during cancellation unwinding, where a hook must not absorb the
> cancellation the caller asked for. Hooks must be synchronous; one that returns
> an awaitable is reported and its result discarded.

### Build prerequisites for native extensions

Building the optional Rust extension from source, for example on a platform
without a native wheel, needs:

- **Rust 1.85 or later**, including `cargo`, because the crates use
  `edition = "2024"`. Install the toolchain with [rustup](https://rustup.rs/),
  which provides `cargo` as well.
- **maturin**, the Rust-to-Python build bridge. The project pins
  `maturin==1.15.0`.

From a source checkout, run `maturin develop` inside the target Python
environment to compile the extension and install it there, then confirm it with
`is_rust_available()` as shown in
[Checking the native extension](#checking-the-native-extension). Pure Python
wheels need no Rust toolchain. Contributors should use `make develop` instead;
the [developers' guide](developers-guide.md#building-the-native-extension)
covers that workflow and distributable wheel builds.

### Troubleshooting the native extension

#### No native wheel for this platform

Pre-built native wheels are published for CPython 3.13 on Linux (x86_64,
aarch64), macOS (x86_64, arm64), and Windows (x86_64). On other platforms and
Python versions, `pip install cuprum` installs the pure Python wheel, which
provides the same functionality without Rust acceleration. To add acceleration
anyway, build the extension as described in
[Build prerequisites for native extensions](#build-prerequisites-for-native-extensions).

#### A forced Rust backend raises `ImportError`

With `CUPRUM_STREAM_BACKEND=rust`, pipeline execution raises `ImportError` when
the extension is not installed, instead of silently using the fallback. Check
the installation with `is_rust_available()`, or set `CUPRUM_STREAM_BACKEND` to
`auto` to fall back to the Python pathway automatically.

#### Hops use the Python fallback on Windows

Windows native wheels remain useful even though every pipeline hop there uses
the Python pump: the restriction protects the event loop's overlapped pipe
handles, and `CUPRUM_STREAM_BACKEND=rust` still runs those hops correctly
through the fallback. On other platforms, a hop that falls back records its
reason as described in
[Why a hop fell back to Python](#why-a-hop-fell-back-to-python).

#### Benchmark results show little difference

Small payloads show little difference between the backends, because the
overhead the Rust pump avoids is per chunk. `splice()` acceleration is
Linux-only. Benchmark comparisons report speed-up as `python_mean / rust_mean`,
so values above `1.0x` mean Rust was faster. The
[developers' guide](developers-guide.md#reading-benchmark-results) explains how
to read the full benchmark output.

## Glossary

- **Allowlist:** the set of programs a context permits to run. The default
  context permits every program; a scope narrows it. The catalogue separately
  controls which programs can be built into commands.
- **Builder:** the callable that `sh.make()` returns. Calling it with arguments
  produces a `SafeCmd`.
- **Catalogue:** a `ProgramCatalogue`, the set of programs for which builders
  may be created, grouped into projects with metadata.
- **Descriptor:** an operating-system handle for an open pipe or file. The Rust
  pump borrows the descriptors of the pipes it connects.
- **End of file (EOF):** the signal that a pipe's writer has closed and no more
  data will arrive.
- **Execution ID (`exec_id`):** a token shared by every event from one
  execution, used to correlate events with each other and with trace spans.
- **Global interpreter lock (GIL):** the CPython lock that lets only one thread
  run Python code at a time. The Rust pump moves data without holding it.
- **Hop:** one transfer of data from a pipeline stage's stdout to the next
  stage's stdin.
- **Observe hook:** a callable registered with `sh.observe()` that receives
  every `ExecEvent`.
- **Process ID (PID):** the operating system's number for a running process.
  Numbers are reused, so Cuprum correlates by execution ID instead.
- **Pump:** the component that performs a hop, either in Python or in the
  optional Rust extension.
- **Resident set size (RSS):** the memory a process holds in RAM; Cuprum
  reports the child's peak where the platform measures it.
- **Scope:** a `with scoped(...)` block that narrows the allowlist and can add
  hooks, a timeout, or environment overlays for the code inside it.
