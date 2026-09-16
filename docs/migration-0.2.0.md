# Migration guide for 0.2.0

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
