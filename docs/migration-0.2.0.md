# Migration guide for 0.2.0

## Catalogue-backed scoped contexts

When a scope allowlist should match a `ProgramCatalogue`, pass the catalogue
directly to `scoped(catalogue=catalogue)`; `scoped` derives the allowlist from
its programs. Keep using `scoped(ScopeConfig(...))` when the scope also needs
hook or policy configuration. For examples and nesting behaviour, see the
[Scoped contexts section](users-guide.md#scoped-contexts) in the users' guide.

## Line-level output observation

Cuprum 0.2.0 adds `SafeCmd.lines()` and the `RunOutputOptions.on_line` callback
for observing decoded stdout and stderr lines as they arrive. These APIs are
additive: existing callers using `SafeCmd.run()` or pipeline execution require
no changes.

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

## Single-project catalogue construction

`ProjectSettings.documentation_locations` and `noise_rules` now default to
empty tuples. Callers whose project has no documentation references or output
noise rules can omit both fields:

```python
from cuprum import Program
from cuprum.catalogue import ProgramCatalogue, ProjectSettings

settings = ProjectSettings(name="rust-test-gates", programs=(Program("cargo"),))
catalogue = ProgramCatalogue.from_project(settings)
```

Callers with project metadata can continue to pass `documentation_locations=`
and `noise_rules=` explicitly. When a complete `ProjectSettings` is already
available, `from_project()` removes the repeated
`ProgramCatalogue(projects=(settings,))` wrapper; existing catalogue
construction remains compatible.

## `CommandResult` execution measurements

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

## Aggregate Python stream-operation observation

Cuprum 0.2.0 adds an opt-in observation channel for completed operations in the
pure-Python stream paths. Existing applications do not need to change: no
observer or metric is installed unless the application registers one with
`observe_stream_operation`.

To adopt the channel, register a synchronous hook around the command or
pipeline scope that should be observed:

```python
from cuprum import ECHO, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics
from cuprum.adapters.stream_metrics import stream_operation_metrics_hook
from cuprum.stream_observation import observe_stream_operation

metrics = InMemoryMetrics()
command = sh.make(ECHO)("hello")
with observe_stream_operation(stream_operation_metrics_hook(metrics)):
    result = command.run_sync()
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
`ExecEvent` observation API and Rust-pump observation channel are unchanged.

## Echo-fallback diagnostics

`CommandResult` now exposes handled text-sink echo failures through its
`relay_fallbacks` tuple. Each `RelayFallback` contains the affected stream and
the closed `unicode_encode` error category, so consumers can count or report
per-command fallbacks without parsing log messages. Pipeline stage results
expose the records owned by that stage, with stdout records before stderr
records.

The field is trailing and defaults to `()`, so existing six-argument positional
construction and existing keyword construction remain compatible. Commands that
time out or are cancelled do not produce a result-level diagnostics tuple;
their already-emitted echo events remain available through `observe_echo`. The
warning, echo event, and result record carry only bounded categorical values
and never include output, sink details, exception objects, or command arguments.

## Idle heartbeat for quiet children

Cuprum 0.2.0 also adds an opt-in idle heartbeat. `RunOutputOptions.idle_after`
defaults to `None`, so the feature is off by default: existing applications
need no change, and a run with no interval creates no timer and no watchdog
task. When set, `idle_after` is a strictly positive, finite number of seconds
of silence on the monitored streams before a notification is due. Further
notifications repeat once per further interval of silence, and any non-empty
read on a monitored stream resets the timer.

To adopt the heartbeat, set the interval on the run's output options:

```python
from cuprum import Program, RunOutputOptions, sh

cmd = sh.make(Program("cargo"))("build", "--locked")
result = cmd.run_sync(output=RunOutputOptions(idle_after=30.0))
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
```

A separate asyncio task is not enough because draining that queue still runs on
the run's own loop. An ordinary exception from the callback, or a failed
diagnostic write, disables further notifications for that run, emits one
sanitized warning, and leaves the child's exit status and captured output
untouched. `KeyboardInterrupt` and `SystemExit` are never suppressed.

## Opt-in presentation sink

Cuprum 0.2.0 adds an opt-in presentation sink,
`cuprum.sinks.GitHubActionsSink`, which frames a run's echoed output in a
GitHub Actions collapsible log group and turns a failed run into one
`::error::` annotation. Existing applications do not need to change: nothing is
framed unless a sink is passed, and capture, success semantics, and the
returned result are unchanged.

To adopt the sink, pass it through `RunOutputOptions` on the `SafeCmd` or
`Pipeline` to be framed:

```python
from cuprum import ECHO, RunOutputOptions, sh
from cuprum.sinks import GitHubActionsSink

command = sh.make(ECHO)("hello")
result = command.run_sync(
    output=RunOutputOptions(echo=True, sink=GitHubActionsSink()),
)
```

`GitHubActionsSink` activates automatically when the parent process runs on
GitHub Actions, which is when `GITHUB_ACTIONS` holds the runner's value `true`.
For local reproduction of CI framing, or on a non-standard runner that does not
export the variable, pass `force=True` to activate the sink deliberately.
