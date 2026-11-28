# Architectural decision record (ADR) 031: Expose bounded live byte streams through an owned handle

Renumbered from 0030 on 2026-11-28 (duplicate number).

## Status

Accepted on 2026-11-27. Cuprum adds `SafeCmd.start_bytes()`, returning a
`RunningBytesCommand` whose `stdout` and `stderr` are independent
`ByteStreamReader`s over a lossless, byte-bounded per-stream queue, while the
handle retains the existing owned lifecycle and settles to a
`BytesCommandResult`.

## Date

2026-11-27.

## Context and problem statement

Cuprum exposes three ways to observe a child's output. `run()` waits for the
child and returns decoded text. `lines()` yields decoded text lines while the
child runs. `run_bytes()` waits for the child and returns its bytes untouched.
An owned `RunningCommand` keeps a child alive and lets a caller stop it, but
its output is available only after settlement.

A consumer that implements a scripted process fake needs something none of
those provides: the bytes a *running* child produces, before the child exits,
without decoding them into lines, while retaining owned control over the child,
its stdin, and its cleanup. Such a consumer must observe a byte prefix before
releasing a barrier, write stdin before closing it, observe stdout reaching
end-of-file while stderr and the child remain live, and cancel while a writer
is blocked. A child can emit invalid UTF-8, split a multibyte character, omit a
newline, or truncate a protocol record, so line observation is not equivalent.
Completed byte capture does not help either: it is not a live interface.

The question is where live byte delivery belongs. Reading the underlying
`asyncio.StreamReader` directly from the consumer would put a second reader on
a pipe the capture pump is already draining, so the two would split the stream
unpredictably and capture would stop reflecting the child's output. A new
public raw-subprocess implementation would duplicate the allowlist, argv,
environment, process-group, cancellation, and reaping rules the library already
owns.

## Decision drivers

- One owner for the process, stdin writer, the two drains, the supervisor, and
  settlement. No second reader may race the capture pump.
- Delivery is lossless while a reader is live: a producer waits for capacity
  rather than dropping a byte.
- Per-stream order is guaranteed; a total cross-stream order is not, and the
  child's write boundaries are not promised to equal the parent's read
  boundaries.
- Each stream reaches end-of-file independently of the other stream and of the
  process's exit.
- Reader closure, settlement, timeout, cancellation, and drain failure all
  settle owned readers and writers and reap the child.
- The live queue has an explicit byte bound, and any loss is reported with a
  terminal status rather than silently shortening the stream.
- Existing text capture, echo, line observation, and byte-result contracts are
  unchanged.
- The Python implementation is the reference; no new native code or
  dependency is introduced.

## Options considered

1. **Return raw `asyncio.StreamReader`s from the handle.** Rejected. Two
   readers would race the capture pump, splitting the stream and hiding bytes
   from capture. It would also expose an asyncio transport as public API, which
   is a platform and implementation detail rather than a Cuprum contract.

2. **Add an `iter_bytes()` async generator to `RunningCommand`.** Rejected as
   the primary interface. An async generator can own one stream's iteration but
   cannot express two independent streams, stdin control, settlement, or
   cancellation without becoming a handle. It also makes the text handle's
   result type conditional.

3. **Extend `RunningCommand` with optional byte readers.** Rejected. Every
   existing annotation and the `start()` return contract would widen to a
   union, and a text caller could receive a handle whose output mode depends on
   a flag rather than on the entry point it called.

4. **Add `SafeCmd.start_bytes()` returning a separately typed
   `RunningBytesCommand`.** Chosen. The entry point makes the output mode and
   the ownership transfer visible at the call site, keeps every text annotation
   unchanged, and gives the byte mode its own result and reader surface.

5. **Read the pipes through a second library-owned pump.** Rejected. It would
   be a parallel raw-subprocess implementation: a second place where capture,
   echo, line observation, and drain cancellation must agree.

## Decision outcome

`SafeCmd.start_bytes()` mirrors `SafeCmd.start()`: it enforces the allowlist,
defaults the execution context, resolves the default wait deadline, accepts a
`StdinSource`, rejects `on_line` and non-pipe output targets before a child
exists, defaults capture on, honours an explicit opt-out, and marks the run
byte-exact. It returns a `RunningBytesCommand`, which owns the same process
record, settlement task, stop lock, control operations, and `async with`
teardown as `RunningCommand`. Its `wait()` and `result` are typed to
`BytesCommandResult`.

The live surface is two `ByteStreamReader` objects, `handle.stdout` and
`handle.stderr`. A reader is an async iterator of `ByteChunk`, which carries
the stream name, the bytes, and a per-stream sequence number. A reader also
exposes `wait_eof()`, an `at_eof` predicate, a `closure` property, and
`aclose()`. `StreamClosure` has four members:

- `EOF`: the drain observed the stream's end, finalized its capture
  successfully, and every byte reached the queue or was queued before the
  reader was released.
- `READER_CLOSED`: the consumer closed the reader; queued items are discarded
  and later live bytes are dropped from the reader while capture continues.
- `TRUNCATED_BY_SETTLEMENT`: settlement began before the reader reached a
  clean end, so later live bytes may exist only in the capture.
- `FAILED`: the drain or the supervisor failed; iteration and `wait_eof()` raise
  a chained `LiveStreamError`. A failure outranks any terminal state already
  published, including a clean `EOF`, and is published by the drain task's own
  completion so a reader is never left waiting for settlement to notice it. A
  live failure also requests an immediate serialized stop, so a held handle
  does not leave its child blocked on a pipe nobody drains.

Normal terminal statuses drain queued items before iteration ends. `FAILED`
takes precedence over pending items: a consumer must see the failure rather
than a silently shortened stream.

Delivery is a per-stream queue with a byte bound. The queue accounts for actual
byte lengths, splits an oversized chunk into ordered pieces, and blocks a `put`
until capacity is available. The drain feeds it from the existing single
`on_chunk` slot, after capture and echo and before the line feeder, so capture,
echo, live bytes, and line observation see every chunk in one order. The read
size used by a byte run is capped at
`min(configured_read_size, max_buffered_bytes)` so a normal read fits the
queue's reservation without splitting.

EOF is signalled out of band by a new synchronous `on_eof` hook on the drain,
called only when the drain observes the empty read and before its own
finalization. It is not called for cancellation, failure, or a preserving
line-stream finalization. The hook only notes the end: the reader's terminal
state is published when the drain task completes successfully, so a
finalization failure publishes `FAILED` rather than leaving a reader with an
EOF it observed before the failure.

At settlement the byte owner releases both queues *before* it waits for the
child: blocked `put`s wake and stop waiting, a chunk that still fits is
enqueued, and only the remainder is discarded rather than blocking teardown,
while capture continues. Releasing first is what keeps a stalled consumer from
holding the child open and making the wait itself deadlock. A reader whose
clean EOF arrives during the reconciliation window publishes `EOF` only if
nothing was discarded; a reader that never reaches EOF publishes
`TRUNCATED_BY_SETTLEMENT`. This keeps a normal child exit honest while ensuring
a stalled consumer can never hold settlement open.

The settlement task is the supervisor. It races the child's exit against the
stdin writer and both stream consumers using the existing rendezvous precedence
(a settled exit, then the writer, then stdout, then stderr). An early consumer
failure stops the child through the existing exit-wait cancellation, is
published as `LiveStreamError` on that reader, and is propagated through
settlement. A producer failure remains a `StdinSourceError`. While the handle
is merely held, a failing drain or producer also requests an immediate
serialized stop; settlement raises the recorded cause even when that stop makes
the child exit before the rendezvous can report it. A child-side stdin close
remains a non-fatal observation. A cancelled `wait()` caller cancels only its
own await: the shielded settlement keeps running, and a later wait, control
operation, or context exit settles the handle. Output transports are released
after byte reconciliation, inside the same shield, so a cancelled teardown
cannot leak a descriptor.

`BoundedDelivery`, the Rust consume path, pipelines, and a public queue type
are out of scope. The queue is an implementation detail, and echo keeps its
existing `BoundedDelivery` policy: the live queue bounds observation, not
presentation.

## Consequences

### Positive

- A caller can consume a running child's bytes incrementally, with owned
  lifecycle control, without touching raw subprocesses or private descriptors.
- Capture, echo, line observation, and live delivery share one drain, so they
  cannot disagree about what the child wrote.
- Losslessness has a clear boundary: no loss while live, explicit
  `TRUNCATED_BY_SETTLEMENT` after settlement.
- Existing text and byte-result contracts are untouched.

### Negative

- A consumer that stops reading applies backpressure to the child. This is the
  price of losslessness while live, and is documented.
- Settlement can discard live-only bytes that did not fit the queue; those
  bytes remain in capture, and the reader status says so.
- A byte run holds up to two queue bounds plus the capture policy's retention,
  so a caller must choose a bound deliberately.

### Limitations

- Per-stream order only. No total cross-stream order is promised, and read
  boundaries are not child write boundaries.
- POSIX process-group containment is bounded: a descendant that leaves the
  group is outside the guarantee, exactly as it is for `OWN_GROUP` elsewhere.
- On Windows, a timed-out supervised test can kill the direct child but cannot
  clean up a descendant that inherited a pipe.
- `aclose()` is cooperative. A producer blocked on a full queue is woken by the
  release, but caller code awaiting the drain is not forcibly stopped.
