# Migration guide for 0.2.0

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
