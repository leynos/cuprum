# Migration guide for 0.2.0

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
