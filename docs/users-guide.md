# cuprum Users' Guide

## Program catalogue

Cuprum exposes a typed `Program` NewType to identify executables. The library
ships with a curated catalogue (`DEFAULT_CATALOGUE`) that defines an allowlist
of programs and project metadata. Requests for unknown executables raise
`UnknownProgramError` so accidental shell access is blocked by default.

- Import curated programs from `cuprum` (for example `ECHO`, `LS`).
- Each project in the catalogue includes `noise_rules` (output lines that
  downstream loggers can safely drop) and `documentation_locations` (links for
  operators and reviewers).

```python
from cuprum import DEFAULT_CATALOGUE, ECHO

entry = DEFAULT_CATALOGUE.lookup(ECHO)
print(entry.project.noise_rules)
```

### Adding project-specific programs

Use `ProjectSettings` and `ProgramCatalogue` to extend or replace the default
catalogue:

```python
from cuprum import Program, ProgramCatalogue, ProjectSettings

project = ProjectSettings(
    name="data-pipeline",
    programs=(Program("python"),),
    documentation_locations=("docs/pipelines.md",),
    noise_rules=(r"^progress:",),
)
catalogue = ProgramCatalogue(projects=(project,))
```

Downstream services can fetch metadata via `catalogue.visible_settings()` to
propagate noise filters and documentation links alongside the allowlist.

### Handling duplicate catalogue entries

Catalogue construction rejects ambiguous project metadata before any command is
built. If two project entries use the same name, `ProgramCatalogue` raises
`DuplicateProjectError`; the exception carries the duplicated name in its
`project_name` attribute. If two projects claim the same `Program`, catalogue
construction raises `DuplicateProgramError`; the exception carries the contested
`program` and the first owning project name in `owner`.

Both duplicate exceptions subclass `ValueError`, so existing configuration
loading code that catches `ValueError` continues to work while newer callers
can inspect the structured payloads directly.

## Typed command core

Cuprum provides `sh.make` to build typed `SafeCmd` instances from curated
programs. Builders enforce the catalogue allowlist up front and carry project
metadata alongside argv, so downstream services can apply noise rules or link
to documentation without a second lookup.

- `sh.make` raises `UnknownProgramError` when the program is not in the current
  catalogue.
- Positional arguments are stringified with `str()`.
- Keyword arguments become `--flag=value` entries with underscores in flag
  names converted to hyphens.
- `None` is rejected; decide whether to skip or substitute a flag before
  calling the builder.

```python
from cuprum import ECHO, sh

echo = sh.make(ECHO)
cmd = echo("-n", "hello world")
print(cmd.argv_with_program)  # ('echo', '-n', 'hello world')
print(cmd.project.noise_rules)  # metadata for downstream loggers
```

### Writing project-specific builders

Wrap `sh.make` in project modules to centralize validation and expose a clear
API for callers:

```python
from pathlib import Path

from cuprum import Program, SafeCmd, sh

SAFE_CAT = Program("cat")


def _safe_path(path: Path) -> str:
    path = path.resolve()
    if not path.is_file():
        msg = f"{path} is not a readable file"
        raise ValueError(msg)
    return path.as_posix()


def cat_file(path: Path, numbered: bool = False) -> SafeCmd:
    args: list[str] = []
    if numbered:
        args.append("-n")
    args.append(_safe_path(path))
    return sh.make(SAFE_CAT)(*args)
```

Builders keep argv construction in one place, making it easier to validate
inputs, document behaviour, and reuse the same allowlisted program across a
codebase.

### Inspecting argv construction

Use `sh.build_argv` when code needs to inspect Cuprum's argument normalization
without creating a command builder or consulting a catalogue. The helper uses
the same argument rules as `sh.make`: positional arguments are stringified in
order, keyword arguments become `--flag=value` entries, underscores in keyword
names become hyphens, and `None` raises `TypeError`.

```python
from cuprum import sh

argv = sh.build_argv("status", porcelain=True, branch="main")
print(argv)  # ('status', '--porcelain=True', '--branch=main')
```

### Core builders for common tools

Cuprum ships a small builder library for common tools in `cuprum.builders`.
These builders are optional but provide a consistent, typed entry point for
git, rsync, and tar commands.

The library includes typed argument helpers:

- `safe_path()` produces a `SafePath` by validating filesystem paths. It
  rejects empty strings, NUL characters, and `..` segments. By default it
  requires absolute paths; set `allow_relative=True` to permit relative paths.
- `git_ref()` produces a `GitRef` by validating git ref names. It rejects
  whitespace, leading `-`, `..`, `//`, `@{`, trailing `.lock`, trailing `.`,
  and refs with characters outside `[A-Za-z0-9._/-]`.

Builder functions validate inputs internally, so callers may pass `str` or
`Path` values directly, or call the helper functions for explicitness.

```python
from pathlib import Path

from cuprum.builders import (
    Compression,
    RsyncOptions,
    TarCreateOptions,
    git_checkout,
    rsync_sync,
    tar_create,
    tar_extract,
)

git_cmd = git_checkout("main", create_branch=True)
rsync_cmd = rsync_sync(
    Path("/srv/data"),
    Path("/backups/data"),
    options=RsyncOptions(archive=True, delete=True),
)
tar_cmd = tar_create(
    Path("/backups/data.tar.gz"),
    [Path("/srv/data")],
    options=TarCreateOptions(compression=Compression.GZIP),
)
restore_cmd = tar_extract(
    Path("/backups/data.tar.gz"),
    destination=Path("/srv/restore"),
)
```

Relative paths require `allow_relative=True` on the relevant option objects
(for example, `RsyncOptions`) or using `safe_path(..., allow_relative=True)`
before passing the result into a builder.

## Pipeline execution

Compose `SafeCmd` instances into a `Pipeline` via the `|` operator. Pipelines
stream data from each stage's stdout into the next stage's stdin and apply
backpressure using `asyncio`'s pipe `drain()` semantics.

Running a pipeline returns a `PipelineResult` that exposes:

- The captured output of the final stage (via `result.stdout`).
- Per-stage exit metadata (via `result.stages`).

```python
import sys
from pathlib import Path

from cuprum import (
    ECHO,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    ScopeConfig,
    scoped,
    sh,
)

PYTHON = Program(str(Path(sys.executable)))
project = ProjectSettings(
    name="pipeline-example",
    programs=(ECHO, PYTHON),
    documentation_locations=(),
    noise_rules=(),
)
catalogue = ProgramCatalogue(projects=(project,))

echo = sh.make(ECHO, catalogue=catalogue)
python = sh.make(PYTHON, catalogue=catalogue)

pipeline = echo("-n", "hello") | python(
    "-c",
    "import sys; sys.stdout.write(sys.stdin.read().upper())",
)

with scoped(ScopeConfig(allowlist=catalogue.allowlist)):
    result = pipeline.run_sync()

print(result.stdout)  # "HELLO"
print([stage.exit_code for stage in result.stages])  # per-stage exit codes
```

Notes:

- Only the final stage's stdout is captured; intermediate stage stdout is
  streamed and represented as `None` in `result.stages`.
- `Pipeline.run` / `run_sync` accept the same `output: RunOutputOptions`
  parameter as `SafeCmd.run` / `run_sync`, so generic helpers can call either
  type with one convention. The flat `capture` / `echo` keyword arguments are
  deprecated and emit a `DeprecationWarning`.
- `RunOutputOptions(echo=True)` echoes the final stage stdout and all stage
  stderr streams to their configured sinks.
- Pipelines fail fast: when a stage exits non-zero, Cuprum terminates the
  remaining stages. The failing stage is available via `result.failure` /
  `result.failure_index`.

## Execution runtime

`SafeCmd.run` executes curated commands asynchronously with predictable capture
and echo semantics and returns a structured `CommandResult`:

- `stdout` and `stderr` are captured by default. Set
  `output=RunOutputOptions(capture=False)` to stream only; the result will carry
  `None` for output fields.
- `output=RunOutputOptions(echo=True)` tees stdout/stderr to the parent process
  while still capturing them when `capture=True`.
- Pass an `ExecutionContext` via the `context` parameter to override execution
  details:
  - `env` overlays key/value pairs on top of the current environment without
    mutating `os.environ`; use it to pass per-command settings.
  - `cwd` sets the working directory for the subprocess when provided.
  - `cancel_grace` controls how long Cuprum waits after `SIGTERM` (termination
    signal) before escalating to `SIGKILL` (kill signal).
  - `timeout` sets a default wall-clock limit in seconds when the call does not
    pass an explicit `timeout` parameter.
  - `stdout_sink` and `stderr_sink` route echoed output to alternative text
    streams when `echo=True`.
  - `encoding` and `errors` configure how captured output is decoded; defaults
    are `"utf-8"` with `"replace"`.
    The same settings are used when `StdinInput.text` is encoded for
    subprocess stdin.
- `exit_code`, `pid`, and `ok` on the `CommandResult` make it easy to branch on
  success.

### Output options

`RunOutputOptions` is the public contract for command output handling on
`SafeCmd.run()` and `SafeCmd.run_sync()`. Pass it with the `output` parameter
when a call needs output behaviour that differs from the default.

- `capture=True` stores stdout and stderr on the returned `CommandResult`.
  Set it to `False` when the caller does not need captured output; `stdout` and
  `stderr` will then be `None`.
- `echo=False` keeps output out of the parent process streams. Set it to `True`
  to tee stdout and stderr while the command runs. When `capture=True`, echoed
  output is still captured.

`RunOutputOptions(capture=True, echo=False)` is the default; you only need to
supply it explicitly when overriding either flag.

```python
from cuprum import ECHO, ExecutionContext, RunOutputOptions, sh


async def greet() -> None:
    cmd = sh.make(ECHO)("-n", "hello runtime")
    ctx = ExecutionContext(env={"GREETING": "1"})
    result = await cmd.run(output=RunOutputOptions(echo=True), context=ctx)
    if not result.ok:
        raise RuntimeError(f"echo failed: {result.exit_code}")
    print(result.stdout)
```

### Migrating from `capture`/`echo` keyword arguments

`IOOptions` is a deprecated alias for `RunOutputOptions`; keep using
`RunOutputOptions` in new code.

Prior to this release, `Pipeline.run()` and `Pipeline.run_sync()` accepted
`capture` and `echo` directly:

```python
# Before
result = pipeline.run_sync(capture=False, echo=True)

# After
from cuprum import RunOutputOptions

result = await cmd.run(output=RunOutputOptions(capture=True, echo=False))
result = cmd.run_sync(output=RunOutputOptions(capture=False))
result = pipeline.run_sync(output=RunOutputOptions(capture=False, echo=True))
```

If existing code constructs `IOOptions`, replace it with `RunOutputOptions`.
`IOOptions` remains a deprecated alias for the same `capture` and `echo` values
and emits a `DeprecationWarning` on construction. Migrate by replacing
`IOOptions(capture=..., echo=...)` usage with
`RunOutputOptions(capture=..., echo=...)` passed as `output=...`.

`RunOutputOptions(capture=True, echo=False)` is the default. Supply it
explicitly only when overriding either flag.

The flat `capture` / `echo` keyword arguments on `Pipeline.run` / `run_sync`
remain accepted for backwards compatibility but emit a `DeprecationWarning`;
passing them together with `output` raises `ValueError`.

If the awaiting task is cancelled while a command is running, Cuprum sends
`SIGTERM` to the subprocess, waits for a short grace period, and then escalates
to `SIGKILL` to ensure the child process is cleaned up.

### Direct stdin input

Use `StdinInput` on `run()` / `run_sync()` to feed data directly to a command's
standard input without adding an extra allowlisted program to a pipeline.
`StdinInput(text=...)` is encoded with the execution context's `encoding` and
`errors` settings. `StdinInput(data=...)` writes the supplied bytes unchanged.
Supplying both values is invalid.

```python
from cuprum import GIT, StdinInput, sh

cmd = sh.make(GIT)("hash-object", "--stdin")
result = cmd.run_sync(stdin=StdinInput(text="hello\n"))
print(result.stdout)
```

If the subprocess closes its stdin pipe before all data is written (e.g. `head`
reads only the first lines), the write failure is silently recorded as a
`stdin_error` trace event and execution continues normally.

### Timeouts

Use the `timeout` parameter on `run()` / `run_sync()` to enforce a wall-clock
limit in seconds. Timeouts are opt-in; when left as `None` no limit is
enforced. When a timeout expires, Cuprum terminates the subprocess, waits for
`cancel_grace`, escalates to `SIGKILL` if needed, and raises `TimeoutExpired`
(mirroring `subprocess.TimeoutExpired`).

Timeout resolution order:

- Explicit `timeout` argument on `run()` / `run_sync` when not `None`.
- `ExecutionContext.timeout` when provided and not `None`.
- `ScopeConfig(timeout=...)` default set via `scoped()` when present.

Example usage:

```python
from cuprum import ECHO, ScopeConfig, TimeoutExpired, scoped, sh

cmd = sh.make(ECHO)("-n", "hello")

with scoped(ScopeConfig(timeout=3.0)):
    try:
        cmd.run_sync()
    except TimeoutExpired as exc:
        print(f"timed out after {exc.timeout}s")
```

Pipeline timeouts apply to the entire pipeline run; partial output is surfaced
using the same capture rules as successful runs.

### Synchronous execution

For scripts or contexts where async/await is not available, use `run_sync()`:

```python
from cuprum import ECHO, ExecutionContext, RunOutputOptions, sh


def greet() -> None:
    cmd = sh.make(ECHO)("-n", "hello sync")
    ctx = ExecutionContext(env={"GREETING": "1"})
    result = cmd.run_sync(output=RunOutputOptions(echo=True), context=ctx)
    if not result.ok:
        raise RuntimeError(f"echo failed: {result.exit_code}")
    print(result.stdout)
```

`run_sync()` accepts `RunOutputOptions` for synchronous output handling and
returns the same `CommandResult` shape as `run()`. It drives the event loop
internally via `asyncio.run()`.

## Execution context and hooks

Cuprum provides `CuprumContext` to scope allowlists and execution hooks across
your application. Contexts are backed by a `ContextVar`, giving you automatic
isolation across threads and async tasks.

**Upgrade note (v0.2.0):** `scoped()` now accepts a single `ScopeConfig`
argument instead of keyword parameters. Update calls like
`with scoped(allowlist=...)` to `with scoped(ScopeConfig(allowlist=...))`.

When you call `SafeCmd.run()` or `run_sync()`, Cuprum automatically:

1. Checks the current context's allowlist and raises `ForbiddenProgramError` if
   the program is not permitted.
2. Invokes all registered before hooks (in FIFO order) before process execution.
3. Invokes all registered after hooks (in LIFO order) after the process
   completes.

**Empty allowlist behaviour:** When no parent context is established,
`scoped(ScopeConfig())` is the permissive root context (all programs allowed),
which supports adoption by defaulting to non-restrictive execution for
first-time callers. Nested `scoped(ScopeConfig())` calls inherit the current
parent policy, so they cannot widen permissions. If an explicit empty allowlist
is introduced in an already restricted path, that scope remains restrictive and
permits no programs.

### Scoped contexts

Use `scoped(ScopeConfig(allowlist=...))` to establish a narrowed execution
context within a code block:

```python
from cuprum import ECHO, LS, ScopeConfig, scoped

# Start with a base allowlist
with scoped(ScopeConfig(allowlist=frozenset([ECHO, LS]))) as ctx:
    assert ctx.is_allowed(ECHO)  # True
    assert ctx.is_allowed(LS)  # True

    # Narrow further in nested scope
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as inner:
        assert inner.is_allowed(ECHO)  # True
        assert inner.is_allowed(LS)  # False (narrowed out)
```

Key properties of `scoped(ScopeConfig())`:

- At root/no-parent, `ScopeConfig()` remains permissive by default.
- In nested scopes, `ScopeConfig()` inherits the parent allowlist and therefore
  remains restricted when the parent is restricted.
- Context is automatically restored when the block exits, even on exception.

### Accessing the current context

Use `current_context()` or `get_context()` to access the current execution
context:

```python
from cuprum import ECHO, current_context, ScopeConfig, scoped

with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    ctx = current_context()
    if ctx.is_allowed(ECHO):
        print("ECHO is allowed")
```

### Dynamic allowlist extension

Use `allow()` to temporarily add programs to the current context:

```python
from cuprum import ECHO, LS, allow, current_context, ScopeConfig, scoped

with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    # LS is not currently allowed
    assert not current_context().is_allowed(LS)

    # Temporarily allow LS
    with allow(LS):
        assert current_context().is_allowed(LS)

    # LS is no longer allowed after the block
    assert not current_context().is_allowed(LS)
```

For manual control, use the `AllowRegistration` handle directly:

```python
from cuprum import ECHO, LS, allow, current_context, ScopeConfig, scoped

with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    reg = allow(LS)
    assert current_context().is_allowed(LS)
    reg.detach()  # Remove LS from allowlist
    assert not current_context().is_allowed(LS)
```

### Scoped environment overlays

**Breaking behaviour note:** `env(...)` resolves overlays against the live
`os.environ` when a subprocess is spawned, not against an import-time or
scope-entry snapshot. Code that relied on snapshot semantics should pass stable
values explicitly through `env(...)` or the per-call `ExecutionContext.env`
mapping.

Use `env()` to overlay environment variables on top of the live `os.environ`
for the duration of a scope. The overlay is resolved at subprocess spawn time,
not at registration time, so variables added to `os.environ` *after* the scope
is entered (the common `monkeypatch.setenv` case under pytest) remain visible
to subprocesses spawned inside the scope:

```python
import os

from cuprum import GIT, ScopeConfig, env, scoped, sh

os.environ["GIT_AUTHOR_NAME"] = "Cuprum"
os.environ["GIT_AUTHOR_EMAIL"] = "cuprum@example.com"

with scoped(ScopeConfig(allowlist=frozenset([GIT]))):
    with env(PATH="/usr/bin:/usr/local/bin"):
        # The subprocess sees PATH overlaid on the *live* os.environ,
        # including GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL above.
        sh.make(GIT)("commit", "--allow-empty", "-m", "noop").run_sync()
```

Precedence, from lowest to highest, is:

1. The current process's `os.environ`, read at spawn time.
2. The overlay registered via `env(...)` (later entries win when scopes
   nest).
3. The per-call `ExecutionContext.env` mapping.

`env()` accepts both positional mappings and keyword arguments, mirroring
`dict(...)`. The function returns an `EnvRegistration` handle which can be used
as a context manager or detached manually:

```python
from cuprum import current_context, env

reg = env({"DATABASE_URL": "sqlite:///tmp/test.db"}, LOG_LEVEL="DEBUG")
try:
    assert current_context().env_overlay["DATABASE_URL"].startswith("sqlite")
finally:
    reg.detach()
```

This intentionally departs from plumbum's `local.env`, which snapshots
`os.environ` once at module import time and can therefore miss variables that
are set later in the process.

### Before and after hooks

Register hooks to run before or after command execution:

```python
from cuprum import ECHO, before, after, ScopeConfig, scoped, sh


def log_before(cmd):
    print(f"About to run: {cmd.program}")


def log_after(cmd, result):
    print(f"Finished {cmd.program} with exit code {result.exit_code}")


with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    with before(log_before), after(log_after):
        cmd = sh.make(ECHO)("hello")
        # Hooks will be invoked when cmd.run() is called
```

Hook ordering:

- **Before hooks** execute in registration order (FIFO): parent hooks run
  before child hooks.
- **After hooks** execute in reverse order (LIFO): child hooks run before
  parent hooks, enabling cleanup patterns.

Like `allow()`, hook registrations can be detached manually:

```python
from cuprum import before, current_context, ScopeConfig, scoped


def my_hook(cmd):
    pass


with scoped(ScopeConfig()):
    reg = before(my_hook)
    assert my_hook in current_context().before_hooks
    reg.detach()
    assert my_hook not in current_context().before_hooks
```

### Logging hook

Use `logging_hook()` to register paired hooks that emit structured start and
exit events through the standard library `logging` module. The helper wires a
before hook (start) and after hook (exit) into the current context and returns
a registration handle that can be used as a context manager:

```python
import logging

from cuprum import ECHO, logging_hook, ScopeConfig, scoped, sh

logger = logging.getLogger("myapp.commands")

with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    with logging_hook(logger=logger):
        sh.make(ECHO)("-n", "hello logging").run_sync()
```

By default, the hook logs to `logging.getLogger("cuprum")` at `INFO` level. The
logger or the log levels can be overridden via `start_level` and `exit_level`.
Start events include the program and argv; exit events include the program,
pid, exit code, duration, and lengths of captured stdout/stderr (zero when
capture is disabled).

### Structured execution events

For richer observability, register an observe hook with `sh.observe()`. Observe
hooks receive `ExecEvent` values describing:

- `plan` — intent to execute the program (argv/cwd/env resolved).
- `start` — subprocess spawned (pid available).
- `stdout` / `stderr` — decoded output emitted as lines.
- `exit` — subprocess finished (exit code and duration).

Hooks can be used for structured logging, metrics, or tracing without coupling
Cuprum to a specific telemetry library.

```python
from cuprum import ECHO, ExecEvent, ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext


events: list[ExecEvent] = []


def capture(ev: ExecEvent) -> None:
    events.append(ev)


with scoped(ScopeConfig(allowlist=frozenset([ECHO]))), sh.observe(capture):
    ctx = ExecutionContext(tags={"run_id": "demo"})
    sh.make(ECHO)("-n", "hello events").run_sync(context=ctx)

stdout_lines = [ev.line for ev in events if ev.phase == "stdout"]
exit_events = [ev for ev in events if ev.phase == "exit"]
assert "hello events" in stdout_lines
assert exit_events[0].tags["run_id"] == "demo"
```

`ExecutionContext.tags` is merged into each event's `tags` mapping. Cuprum also
adds default tags such as the project name and pipeline stage metadata.

### Thread and async task isolation

`CuprumContext` uses Python's `ContextVar` mechanism, which provides automatic
isolation:

- Each thread gets its own context value.
- Each async task inherits the context from its creator and can modify it
  independently.

This isolation allows `scoped(ScopeConfig())` to be used in concurrent code
without context leaking between threads or tasks:

```python
import asyncio

from cuprum import ECHO, LS, current_context, ScopeConfig, scoped


async def worker(name: str, programs):
    with scoped(ScopeConfig(allowlist=programs)):
        await asyncio.sleep(0.1)  # Simulate work
        ctx = current_context()
        print(f"{name}: ECHO allowed = {ctx.is_allowed(ECHO)}")


async def main():
    await asyncio.gather(
        worker("task1", frozenset([ECHO])),
        worker("task2", frozenset([LS])),
    )
    # task1 sees ECHO allowed, task2 does not


asyncio.run(main())
```

### Checking allowlist membership

Use `is_allowed()` to check if a program is permitted:

```python
from cuprum import ECHO, CuprumContext

ctx = CuprumContext(allowlist=frozenset([ECHO]))
if ctx.is_allowed(ECHO):
    print("ECHO is allowed")
```

Use `check_allowed()` to raise `ForbiddenProgramError` if a program is not
allowed:

```python
from cuprum import ECHO, LS, CuprumContext, ForbiddenProgramError

ctx = CuprumContext(allowlist=frozenset([ECHO]))
try:
    ctx.check_allowed(LS)
except ForbiddenProgramError as e:
    print(f"Access denied: {e}")
```

## Telemetry adapters

Cuprum provides example adapters in `cuprum.adapters` that demonstrate how to
integrate execution events with common observability backends. These adapters
are optional and non-blocking; they do not depend on external telemetry
libraries but define protocols that can be implemented with any backend.

### Structured logging adapter

The `logging_adapter` module provides an observe hook that emits structured log
records for each execution phase. Unlike the simpler `logging_hook()`, this
adapter uses the full `ExecEvent` stream for fine-grained observability.

```python
import logging

from cuprum import ECHO, ScopeConfig, scoped, sh
from cuprum.adapters.logging_adapter import structured_logging_hook

logging.basicConfig(level=logging.DEBUG)

with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    hook = structured_logging_hook()
    with sh.observe(hook):
        sh.make(ECHO)("hello").run_sync()
```

The hook attaches selected `cuprum_*` prefixed extra fields to log records:

- `cuprum_phase`: Event phase (plan, start, stdout, stderr, exit)
- `cuprum_program`: Program being executed
- `cuprum_argv`: Full argument vector
- `cuprum_pid`: Process ID (when available)
- `cuprum_exit_code`: Exit code (for exit events)
- `cuprum_duration_s`: Duration in seconds (for exit events)
- `cuprum_tags`: Event tags as a dictionary

For JSON output suitable for log aggregation systems, use the
`JsonLoggingFormatter`:

```python
import logging

from cuprum.adapters.logging_adapter import JsonLoggingFormatter

handler = logging.StreamHandler()
handler.setFormatter(JsonLoggingFormatter())
logger = logging.getLogger("cuprum.exec")
logger.addHandler(handler)
```

### Metrics adapter

The `metrics_adapter` module provides a Prometheus-style metrics hook that
collects counters and histograms. It uses a protocol class so the backend can
be implemented with any preferred metrics library.

```python
from cuprum import ECHO, ScopeConfig, scoped, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook

metrics = InMemoryMetrics()

with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    with sh.observe(MetricsHook(metrics)):
        sh.make(ECHO)("hello").run_sync()

print(metrics.counters)  # {'cuprum_executions_total': 1.0, ...}
print(metrics.histograms)  # {'cuprum_duration_seconds': [...]}
```

The hook collects:

- `cuprum_executions_total`: Counter incremented on each command start
- `cuprum_failures_total`: Counter incremented on non-zero exit
- `cuprum_duration_seconds`: Histogram of execution durations
- `cuprum_stdout_lines_total`: Counter of stdout lines emitted
- `cuprum_stderr_lines_total`: Counter of stderr lines emitted

All metrics include `program` and `project` labels. Missing, empty, or
explicit `None` project tags fall back to `unknown`.

To integrate with a real metrics library like `prometheus_client`, implement the
`MetricsCollector` protocol:

```python
from prometheus_client import Counter, Histogram

from cuprum.adapters.metrics_adapter import MetricsCollector, MetricsHook


class PrometheusMetrics:
    def __init__(self) -> None:
        self._exec_total = Counter(
            "cuprum_executions_total",
            "Total command executions",
            ["program", "project"],
        )
        self._duration = Histogram(
            "cuprum_duration_seconds",
            "Execution duration",
            ["program", "project"],
        )

    def inc_counter(self, name, value, labels):
        if name == "cuprum_executions_total":
            self._exec_total.labels(**labels).inc(value)

    def observe_histogram(self, name, value, labels):
        if name == "cuprum_duration_seconds":
            self._duration.labels(**labels).observe(value)


hook = MetricsHook(PrometheusMetrics())
```

### Tracing adapter

The `tracing_adapter` module provides an OpenTelemetry-style tracing hook that
creates spans for command execution. It uses protocol classes so you can
implement the backend with your preferred tracing library.

```python
from cuprum import ECHO, ScopeConfig, scoped, sh
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook

tracer = InMemoryTracer()

with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    with sh.observe(TracingHook(tracer)):
        sh.make(ECHO)("hello").run_sync()

span = tracer.spans[0]
print(span.name)  # 'cuprum.exec echo'
print(span.attributes)  # {'cuprum.program': 'echo', ...}
```

The hook creates spans with these attributes:

- `cuprum.program`: The program being executed
- `cuprum.argv`: Full argument vector
- `cuprum.pid`: Process ID
- `cuprum.exit_code`: Exit code (set on span end)
- `cuprum.duration_s`: Duration in seconds (set on span end)
- `cuprum.project`: Project name from tags
- `cuprum.pipeline_stage_index`: Pipeline stage index (if applicable)

Output lines (stdout/stderr) are recorded as span events when `record_io=True`
(the default).

To integrate with OpenTelemetry, implement the `Tracer` and `Span` protocols:

```python
from opentelemetry import trace

from cuprum.adapters.tracing_adapter import Span, Tracer, TracingHook


class OTelSpan:
    def __init__(self, otel_span) -> None:
        self._span = otel_span

    def set_attribute(self, key, value):
        self._span.set_attribute(key, value)

    def add_event(self, name, attributes=None):
        self._span.add_event(name, attributes=attributes or {})

    def set_status(self, *, ok):
        from opentelemetry.trace import StatusCode

        code = StatusCode.OK if ok else StatusCode.ERROR
        self._span.set_status(code)

    def end(self):
        self._span.end()


class OTelTracer:
    def __init__(self, tracer) -> None:
        self._tracer = tracer

    def start_span(self, name, attributes=None):
        span = self._tracer.start_span(name, attributes=attributes)
        return OTelSpan(span)


otel_tracer = trace.get_tracer("cuprum")
hook = TracingHook(OTelTracer(otel_tracer))
```

### Design principles for adapters

The adapters follow these design principles:

1. **Optional dependencies**: Adapters do not import external telemetry
   libraries. They define protocols that can be implemented with any backend.

2. **Non-blocking execution**: Hooks are synchronous and complete quickly.
   For high-throughput scenarios, consider buffering or async handlers.

3. **Protocol-based integration**: Use Python's `Protocol` classes to define
   the interface, making it easy to swap implementations without inheritance.

4. **Reference implementations**: The `InMemoryMetrics` and `InMemoryTracer`
   classes serve as both documentation and test utilities.

## Concurrent command execution

Cuprum provides `run_concurrent` to execute multiple `SafeCmd` instances
concurrently with optional concurrency limits. Results are returned in
submission order, and hooks fire per command to preserve existing semantics.

### Basic usage

```python
from cuprum import ECHO, ScopeConfig, run_concurrent_sync, scoped, sh

echo = sh.make(ECHO)
commands = [echo("-n", f"task-{i}") for i in range(5)]

with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
    result = run_concurrent_sync(*commands)

print(f"All succeeded: {result.ok}")
for cmd_result in result.results:
    print(cmd_result.stdout)
```

For async code, use `run_concurrent`:

```python
import asyncio

from cuprum import ECHO, ScopeConfig, run_concurrent, scoped, sh


async def main() -> None:
    echo = sh.make(ECHO)
    commands = [echo("-n", f"task-{i}") for i in range(5)]

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        result = await run_concurrent(*commands)

    print(f"All succeeded: {result.ok}")


asyncio.run(main())
```

### ConcurrentConfig

Configure execution via the `ConcurrentConfig` dataclass:

```python
from cuprum import ECHO, ConcurrentConfig, run_concurrent_sync, scoped, sh

echo = sh.make(ECHO)
commands = [echo("-n", f"task-{i}") for i in range(10)]

config = ConcurrentConfig(
    concurrency=3,  # At most 3 commands run simultaneously
    capture=True,  # Capture stdout/stderr (default)
    echo=False,  # Do not tee output (default)
    fail_fast=False,  # Continue after failures (default)
)

with scoped(allowlist=frozenset([ECHO])):
    result = run_concurrent_sync(*commands, config=config)
```

Configuration attributes:

- `concurrency`: Maximum parallel commands. `None` (default) runs all in
  parallel; `1` runs sequentially.
- `capture`: When `True` (default), capture stdout/stderr into results.
- `echo`: When `True`, tee output to configured sinks.
- `context`: Shared `ExecutionContext` for all commands.
- `fail_fast`: When `True`, cancel remaining commands after first failure.

When `config` is `None` (the default), `ConcurrentConfig()` is used.

### Limiting concurrency

Pass a `ConcurrentConfig` with `concurrency=N` to limit parallel execution.
This uses an `asyncio.Semaphore` internally:

```python
from cuprum import ECHO, ConcurrentConfig, run_concurrent_sync, scoped, sh

echo = sh.make(ECHO)
commands = [echo("-n", f"task-{i}") for i in range(10)]

with scoped(allowlist=frozenset([ECHO])):
    # At most 3 commands run simultaneously
    result = run_concurrent_sync(*commands, config=ConcurrentConfig(concurrency=3))
```

### Failure handling

By default, `run_concurrent` uses collect-all mode: all commands run to
completion regardless of failures. The `ConcurrentResult.failures` tuple
contains indices of commands that exited non-zero:

```python
from cuprum import run_concurrent_sync, scoped

with scoped(allowlist=...):
    result = run_concurrent_sync(cmd1, cmd2, cmd3)

if not result.ok:
    print(f"Failed command indices: {result.failures}")
    print(f"First failure: {result.first_failure}")
```

### Fail-fast mode

Enable `fail_fast=True` in the config to cancel remaining commands after the
first failure:

```python
from cuprum import ConcurrentConfig, run_concurrent_sync, scoped

with scoped(allowlist=...):
    result = run_concurrent_sync(*commands, config=ConcurrentConfig(fail_fast=True))

if not result.ok:
    print(f"First failure: {result.first_failure}")
    # Remaining commands were cancelled
```

In fail-fast mode, commands that were already running receive cancellation
(SIGTERM (termination signal) then SIGKILL (kill signal) after the grace
period). Commands that had not yet started are not scheduled.

### Hook semantics

Hooks fire per command, preserving consistency with single-command execution:

- **Before hooks** run when each command starts (may be interleaved).
- **After hooks** run when each command completes (may be interleaved).
- **Observe hooks** receive `ExecEvent` values for each command.

Commands share the execution context, so all commands see the same hooks and
allowlist:

```python
from cuprum import ECHO, before, run_concurrent_sync, scoped, sh


def log_start(cmd) -> None:
    print(f"Starting: {cmd.program}")


echo = sh.make(ECHO)
commands = [echo("-n", f"task-{i}") for i in range(3)]

with scoped(allowlist=frozenset([ECHO])), before(log_start):
    # log_start fires for each command
    result = run_concurrent_sync(*commands)
```

### ConcurrentResult properties

The `ConcurrentResult` dataclass provides:

- `results`: Tuple of `CommandResult` in submission order.
- `failures`: Tuple of indices where commands exited non-zero.
- `ok`: `True` when all commands succeeded.
- `first_failure`: The first failed `CommandResult`, or `None` if all
  succeeded.

## Performance extensions (optional Rust)

Cuprum ships as a pure Python wheel by default. Some platforms also provide
native wheels that bundle an optional Rust extension used by stream performance
optimizations. The Rust extension is not required to use Cuprum and does not
change behaviour for pure Python installations.

Cuprum does not use cibuildwheel; native wheels are built with maturin
directly, and the pure Python wheel is built with `uv_build`.

### Checking Rust availability

It is possible to check whether the optional extension is available in the
current environment using the public helper `cuprum.is_rust_available()`, which
wraps the internal probe `_rust_backend.is_available()`. The module
`cuprum._rust_backend` is private and not semver-stable, so production code
should avoid calling `_rust_backend.is_available()` directly:

```python
import cuprum as c

if c.is_rust_available():
    print("Rust extension is available")
else:
    print("Rust extension is not installed")
```

The probe returns `False` on pure Python installations and does not raise when
native wheels are missing.

### Rust stream pump (internal)

The Rust extension now includes an internal pump function exposed as
`cuprum._streams_rs.rust_pump_stream`. This private API is intended for
Cuprum's internal pipeline dispatcher and may change without notice. Public
command execution remains unchanged until the dispatcher integration lands.

### Rust stream consumption (internal)

The Rust extension also exposes `cuprum._streams_rs.rust_consume_stream`, which
reads a file descriptor and returns decoded text. This private API is intended
for the internal stream dispatcher and may change without notice.

`rust_consume_stream` is currently **implemented but not yet integrated**:
unlike the pump side (which is routed through a Rust-then-Python dispatcher),
no production code path routes stream consumption through it, and every consume
uses the pure-Python implementation regardless of the selected backend.
Consume-side dispatch is evidence-gated Phase 2 work in ADR-002. The tee
hot-path profiling baseline now supports a future capture-only dispatcher, but
that dispatcher has not landed yet and will be limited to fd-backed,
UTF-8/replace, capture-only streams without echo sinks or line callbacks.

The Rust consume helper always decodes UTF-8 with replacement semantics for
invalid sequences. Other encodings or error modes require the Python
implementation.

Backend selection for stream operations is controlled by the
`CUPRUM_STREAM_BACKEND` environment variable:

- `auto` (default): use Rust when available, otherwise fall back to Python.
- `rust`: force the Rust pathway and raise `ImportError` if the extension is
  unavailable.
- `python`: force the pure Python pathway.

The backend is resolved once on first use and the result is cached for the
lifetime of the process. Changing the environment variable after the first
resolution has no effect.

### Choosing a stream backend

Most users should leave backend selection on `auto`. This uses the Rust pathway
when the native extension is installed and falls back cleanly to pure Python
otherwise. Select `python` when the pure Python path is required regardless of
wheel availability, for example when debugging, reproducing an issue on a pure
Python installation, or relying on Python-only capture features. Select `rust`
for benchmarking or throughput-heavy workloads and to require the native
extension (fail if unavailable).

Set `CUPRUM_STREAM_BACKEND` before first backend resolution in the process. For
example:

```bash
CUPRUM_STREAM_BACKEND=rust uv run python my_script.py
```

If the environment variable is changed after Cuprum has already resolved the
backend in the current process, the cached result will continue to be used.

The backend selection is active for inter-stage stream pumping in pipelines.
When the Rust backend is selected, data transfer between pipeline stages uses
the Rust extension outside the GIL via `loop.run_in_executor()`. Stream
consumption (stdout/stderr capture) currently always uses the Python pathway
regardless of the backend setting; this ensures line callbacks and echo
features remain available.

Pipeline pumping applies an additional safety guard: if Rust is selected but
Cuprum cannot extract raw file descriptors from asyncio transports for a
specific transfer, it falls back to the Python pump for that transfer. This
fallback is automatic and transparent to callers.

Forced Rust mode is intentionally strict. If `CUPRUM_STREAM_BACKEND=rust` is
set and the Rust extension is unavailable, pipeline execution raises
`ImportError` instead of silently falling back.

Current Rust acceleration applies to inter-stage pipeline pumping. For
high-throughput, multi-stage pipelines, this can reduce per-chunk overhead and
deliver substantial multi-fold throughput improvements on large transfers,
especially on Linux pipe-to-pipe workloads where `splice()` is available. For
small outputs, the difference is often negligible. When stdout/stderr capture,
custom encoding/error handling, or line-oriented callbacks matter more than raw
throughput, prefer `python`. The current backend selection does not change
capture semantics: stdout/stderr capture still uses the Python pathway.

Use `make benchmark-e2e` to measure workload before standardizing on `rust` for
a production path. The benchmark suite gives a better answer than a fixed rule
of thumb when payload size, platform, or callback behaviour differ from the
default scenarios.

For parent-side tee and capture profiling, use the dedicated harness documented
in `benchmarks/README.md`. That harness replays deterministic base64 fixtures
into the parent process and records text-first `perf` artefacts for the
`echo=True` and `capture=True` consumption paths. It is the right tool when
investigating sink write cost, line-callback overhead, capture accumulation, or
the boundary between inter-stage pumping and final stream consumption.

The profiling harness accepts a `BackendSelector` dependency for benchmark
workers that need to force a backend around a single parent-side run. The
default selector mutates `CUPRUM_STREAM_BACKEND` process-wide while holding a
backend lock, clears Cuprum's cached backend choice for the scoped run, and
restores the original environment afterwards. It is intentionally non-reentrant
on the same thread: nested selector activation raises `RuntimeError` rather
than risking a stale environment value or backend cache leak. Avoid wrapping one
`BackendSelector` activation inside another; start a separate worker process
or let the outer selector own the full profiled run.

Both backends are tested for behavioural parity across edge cases including
empty streams, multibyte UTF-8 at chunk boundaries, broken pipes (where the
downstream stage exits before the upstream finishes), and backpressure under
large payloads. The test suite verifies that pipeline output is identical
regardless of which backend is active, so switching between backends does not
change observable behaviour.

The stream test suite also includes property-based coverage using Hypothesis.
These tests generate randomized payload bytes and randomized chunk boundaries,
run them through real pipelines, and assert byte-preservation by comparing
deterministic hexadecimal output. This provides broad regression coverage for
content integrity across both Python and Rust pumping pathways.

Cuprum also tests the Python backend's pure line-callback splitting helpers
directly. Those tests prove that completed lines and final partial lines
account for all generated text, and that recognized line endings are stripped
without editing the rest of the line. In development environments, CrossHair
adds bounded symbolic checks for the same contracts; those checks are skipped
on Python versions where CrossHair cannot trace the active bytecode.

### Benchmark suite

Cuprum includes an opt-in benchmark suite for stream-performance tracking.
Benchmark modules are intentionally excluded from normal `make test` runs unless
`CUPRUM_RUN_BENCHMARKS=1` is set.

Run microbenchmarks (pytest-benchmark):

```bash
make benchmark-micro
```

This executes `benchmarks/test_stream_microbenchmarks.py` and writes benchmark
results to `dist/benchmarks/microbenchmarks.json`.

Run end-to-end throughput benchmarks (hyperfine):

```bash
make benchmark-e2e
```

This executes `benchmarks/pipeline_throughput.py` and writes throughput results
to `dist/benchmarks/pipeline-throughput.json`.

Generate a dry-run benchmark plan without executing hyperfine:

```bash
UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run python \
  benchmarks/pipeline_throughput.py \
  --smoke \
  --dry-run \
  --worker-iterations 20 \
  --output /tmp/pipeline-throughput-plan.json
```

Dry-run output includes scenario metadata, command lines, benchmark profile
metadata, worker iteration count, and whether the Rust extension is available
in the current environment.

The `--worker-iterations` option (default `20`) controls how many pipeline
executions are batched inside a single measured worker process. Higher values
reduce hyperfine timing overhead per pipeline run; the recorded mean is the
total elapsed time for all iterations combined. The worker `--iterations` flag,
which hyperfine scenario commands pass directly, mirrors this setting. Do not
compare ratchet baselines produced with different `worker_iterations` values;
the benchmark profile validation rejects mismatched plans automatically.

Benchmark commands invoke the Python interpreter directly via `python_bin`
(resolved from `sys.executable` at plan-generation time) rather than through
`uv run`, so measured runtimes exclude launcher overhead.

Benchmark iteration options are intentionally bounded to prevent accidental
resource exhaustion in local runs or CI jobs. Pipeline throughput runs accept
`--warmup` and `--runs`; tee profiling runs accept `--warmup-count` and
`--repeat-count`. Each value must be at most `1000`.

Interpretation notes:

- pump-latency microbenchmarks reflect inter-stage pipeline transfer overhead;
- consume-throughput microbenchmarks reflect captured stdout read/decode
  throughput (currently the Python consume path);
- end-to-end hyperfine runs measure batched worker-pipeline runtime and include
  a Rust scenario only when the Rust extension is available.

#### Scenario matrix

The end-to-end throughput suite exercises a systematic matrix of benchmark
scenarios for each available backend:

- payload sizes: small (1 KB), medium (1 MB), and large (100 MB);
- pipeline depth: single-stage (writer|sink, no passthrough) and multi-stage
  (writer|passthrough|sink);
- line callbacks: with (line-by-line sink processing) and without (binary bulk
  read).

This produces 12 scenarios per backend. In smoke mode, payload sizes are
reduced (1 KB, 64 KB, 1 MB) to keep validation fast while exercising the full
matrix shape. Scenarios follow a systematic naming convention:
`{backend}-{size}-{depth}-{callbacks}`, for example `python-small-single-nocb`
or `rust-large-multi-cb`.

### Linux splice() optimization

On Linux, the Rust extension automatically uses the `splice()` system call for
pipe-to-pipe transfers. This provides zero-copy data movement entirely within
the kernel, offering significant throughput improvements for large pipeline
transfers.

The optimization is transparent to users:

- Automatically enabled on Linux when using pipe file descriptors
- Falls back to read/write for unsupported file descriptor types (files, some
  sockets)
- No configuration required

For maximum benefit, ensure pipeline stages use pipes (the default for
`Pipeline` execution) rather than temporary files.

### Build prerequisites for native extensions

Contributors who want to build or develop the optional Rust extension from
source need the following tools installed:

- **Rust toolchain (rustc and cargo) 1.85 or later.** The Rust crate uses
  `edition = "2024"`, which requires Rust 1.85+. Install via
  [rustup](https://rustup.rs/):

  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  ```

  cargo ships as part of the standard rustup installation and does not need to
  be installed separately.

- **maturin** (the Rust-to-Python build bridge). maturin is pinned as a dev
  dependency and is installed automatically when you run `uv sync --group dev`.
  Alternatively, install it manually:

  ```bash
  pip install maturin==1.13.3
  ```

To build and install the Rust extension in development mode, run
`maturin develop` from the project root:

```bash
maturin develop
```

This compiles the Rust crate, links the resulting shared library into the
Cuprum package, and makes the extension available for import. To verify the
extension is working:

```bash
python -c "import cuprum; print(cuprum.is_rust_available())"
```

The command should print `True` when the Rust extension is available.

Pure Python wheels do not require a Rust toolchain. Running `uv build --wheel`
produces a pure Python wheel using `uv_build` without any Rust dependencies.

### Troubleshooting

This section addresses common issues when working with the optional Rust
extension.

**Missing wheels on unsupported platforms.** Pre-built native wheels are
published for common platforms: Linux (x86_64, aarch64), macOS (x86_64, arm64),
and Windows (x86_64, arm64). On other platforms, `pip install cuprum` installs
the pure Python wheel automatically. The pure Python wheel provides the same
functionality without Rust acceleration. Contributors who want Rust
acceleration on unsupported platforms can build from source using the
prerequisites described above and running `maturin develop`.

**Forced fallback behaviour.** The `CUPRUM_STREAM_BACKEND` environment variable
controls which stream implementation is used:

- `auto` (default): uses the Rust pathway when available, otherwise falls
  back silently to the Python pathway.
- `python`: forces the pure Python pathway regardless of whether the Rust
  extension is installed.
- `rust`: forces the Rust pathway and raises `ImportError` if the extension
  is unavailable.

If `CUPRUM_STREAM_BACKEND=rust` is set but the Rust extension is not installed,
pipeline execution raises `ImportError` instead of falling back. To diagnose
whether the extension is available, run:

```bash
python -c "import cuprum; print(cuprum.is_rust_available())"
```

See the "Choosing a stream backend" section above for full details on backend
selection.

**Benchmark result interpretation.** When reading benchmark results, keep the
following in mind:

- Small payloads show negligible difference between Python and Rust pathways.
  The overhead being avoided is per-chunk, and small payloads have few chunks.
- The `splice()` optimization is Linux-only. macOS and Windows use
  read/write loops, so Rust throughput gains are smaller on those platforms.
- The CI comparison summary reports speedup as `python_mean / rust_mean`.
  Values above `1.0x` mean Rust was faster.
- Use `make benchmark-e2e` to measure performance on your specific workload
  and platform before drawing conclusions.

See the "Benchmark suite" section above for scenario definitions and metric
details.

### CI build commands

The continuous integration (CI) workflows run the following checks:

- Type checking and tests run in a Python version matrix. Required rows use
  Python 3.12, 3.13, and 3.14. The Python 3.15a row is experimental and allowed
  to fail.
- Formatting and lint checks run on Python 3.13.
- Coverage upload (artefact + optional CodeScene upload) runs on Python 3.13.
- Benchmark ratchet runs on pull requests and pushes to `main`:
  - It benchmarks the current checkout in smoke mode with a release build of
    the Rust extension.
  - It compares each scenario's within-run `rust_mean / python_mean` ratio
    against the latest successful `main` baseline artefact when one exists, so
    runner-speed differences between CI jobs cancel out.
  - It skips comparison when the saved baseline uses an older benchmark profile
    shape because single-run and batched-worker timings are not comparable.
  - Its baseline fetch helper follows GitHub’s signed archive redirects
    without forwarding GitHub-only authentication headers to the storage host.
  - It generates a Python-versus-Rust comparison report from the candidate
    smoke artefacts and appends the same Markdown table to the GitHub Actions
    workflow summary.
  - It uploads candidate JSON artefacts plus `ratchet-report.json` and
    `comparison-report.json`.
  - On pushes to `main`, it also publishes the new smoke benchmark JSON as the
    next baseline artefact for future runs.
  - If no previous `main` baseline exists yet, it records a bootstrap skip
    report instead of failing the workflow.
  - It fails when any scenario pair has
    `(candidate_ratio - baseline_ratio) / baseline_ratio > 0.30`, where each
    ratio is `rust_mean / python_mean` from the same benchmark run.

The workflow summary table is derived from the filtered candidate smoke plan
and throughput JSON. Rows are matched by the shared scenario label (
`small-single-nocb`, `small-single-cb`, and so on) and include:

- Python mean runtime in seconds
- Rust mean runtime in seconds
- speedup ratio `python_mean / rust_mean`
- faster backend (`python`, `rust`, or `tie`)

Interpretation:

- `2.00x` means the Python run took twice as long as the Rust run, so Rust was
  faster.
- `1.00x` means the two backends were effectively tied for that scenario.
- `0.80x` means Python was faster for that scenario.

- Pure Python wheel:

```bash
uv build --wheel --out-dir dist
```

- Native wheel (per platform):

```bash
maturin build --release --out wheelhouse \
  --manifest-path rust/cuprum-rust/Cargo.toml
```

For Linux wheels, the native build runs inside a manylinux-compatible container
and uses a matching compatibility tag plus explicit interpreter selection:

```bash
maturin build --release --manylinux 2_28 \
  -i python3.13 --out wheelhouse \
  --manifest-path rust/cuprum-rust/Cargo.toml
```

The CI workflow supplies `manylinux 2_28` via the maturin action configuration
to ensure the resulting wheel tags are manylinux-compatible.

### Verification procedure

The canonical verification sequence is:

1. Install the pure Python wheel and confirm the Rust probe returns `False`.
2. Force-reinstall the native wheel and confirm the Rust probe returns `True`.
3. Compare metadata (name, version, requires-python, dependencies, and
   classifiers) across the two installs to detect drift.
