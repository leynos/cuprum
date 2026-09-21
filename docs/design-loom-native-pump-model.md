# Native-pump Loom model

## Purpose and boundary

This document records the production correspondence for the bounded Loom model
in `rust/cuprum-rust/tests/loom.rs`. It is the concurrency-testing companion to
the unsafe-boundary work in [issue #379][issue-379]. The model reuses the Rust
borrowed-reader discipline and pump state machine, while representing Python
and the operating system as explicit actors. It does not add production
threads, atomics, or locks: the Rust stream operation is synchronous and
single-threaded once the Python executor worker begins it.

Loom does not model asyncio scheduling, the CPython GIL, kernel file
descriptors, or uninstrumented dependencies. The event loop and kernel are
therefore bounded environment actors. A passing model establishes stated safety
properties of the modelled ownership protocol, not liveness of a blocking
kernel read or fairness of an arbitrary scheduler.

## Actors, resources, and linearization points

| Actor               | Production source                                                          | Model action                      | Linearization point                                                    |
| ------------------- | -------------------------------------------------------------------------- | --------------------------------- | ---------------------------------------------------------------------- |
| Event-loop task     | `_run_rust_pump_with_blocking_fds` in `_pipeline_stream_native_cleanup.py` | submission and cancellation actor | setting `was_cancelled` or submitting the worker                       |
| Executor worker     | `_submit_rust_pump` and `rust_pump_stream`                                 | native worker actor               | accepting the writer duplicate, then reporting a terminal I/O outcome  |
| Completion callback | `_complete_rust_pump` and `_finalize_native_pump_resources`                | completion/observer actor         | `_cleanup_lock` admits the first cleanup and sets `_cleanup_completed` |

_Table 1: Production actors and model linearization points._

| Shared resource          | Production representation                | Model representation                                           | Ownership transition                                                    |
| ------------------------ | ---------------------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Callback/state writer FD | `_RustPumpState.writer_fd`               | `DescriptorRecord` close log                                   | callback-owned; closes exactly once                                     |
| Native-worker writer FD  | `_NativePumpFds.writer_fd`               | `DescriptorRecord` and `PumpCloseCounts.writer_closes`         | unallocated on failed hand-off; worker-owned and closes once on success |
| Borrowed reader FD       | `with_borrowed_reader` in `lib.rs`       | `ModelFd`, `ManuallyDrop`, and `PumpCloseCounts.reader_closes` | remains caller-owned; never closes                                      |
| Blocking-mode guard      | `_BlockingModeGuard`                     | `blocking_restored` flag                                       | restored only after worker settlement                                   |
| Cancellation             | `_RustPumpState.was_cancelled`           | Loom atomic                                                    | event loop records request before classification                        |
| Cleanup-once state       | `_cleanup_completed` and `_cleanup_lock` | Loom atomic plus Loom mutex                                    | first settled cleanup wins                                              |
| Completion signal        | executor-future settlement               | Loom `Condvar` and predicate                                   | observer rechecks settlement before cleanup                             |

_Table 2: Production resources and model ownership transitions._

The model invokes `pump_machine::advance` for normal and downstream-close
outcomes, and invokes the same `model_pump_stream` helper that mirrors
`with_borrowed_reader` and its `ManuallyDrop` discipline. The remaining Python
callbacks, descriptor duplication, and transport operations are environment
actions because Loom cannot schedule them directly. The audited native
ownership bridge returns per-path reader and worker-writer close counts. The
lifecycle record uses the worker-writer count for its native ownership
assertion, while the callback/state writer remains separately tracked and
asserted.

## Model set and bounds

Each model has at most four actors: the initiating event-loop task, one native
worker, and up to two completion observers. The normal and downstream-close
traces use at most three pump-machine transitions; failed hand-off and
native-failure traces use no synthetic I/O. The driver sets
`LOOM_MAX_PREEMPTIONS`, `LOOM_MAX_BRANCHES`, and `LOOM_MAX_THREADS`; the smoke
lane uses 2, 300, and 4, while daily/manual runs use 3, 2,000, and 4. Reaching
a Loom bound is a failed or incomplete result, never evidence of exhaustive
exploration.

The assertions are safety properties: the callback/state writer closes once, a
successful hand-off gives the native worker a distinct writer that closes once,
and a failed hand-off leaves no native owner. The reader remains borrowed,
terminal cleanup runs at most once, and no cleanup releases resources while the
worker is active. Joining the bounded actors establishes completion only under
the model's progress assumption. It does not establish that a genuinely
blocking read returns, or that an unfair scheduler eventually runs a
participant.

The explicitly selected `loom-defect-fixture` feature injects a second
native-worker writer close. Its `#[should_panic]` harness demonstrates that the
duplicate-close assertion detects the representative defect without making
normal CI fail.

[issue-379]: https://github.com/leynos/cuprum/issues/379
