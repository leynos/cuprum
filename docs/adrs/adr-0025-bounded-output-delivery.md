# Architectural decision record (ADR) 025: Bounded output delivery

## Status

Accepted on 2026-10-12.

## Date

2026-10-12.

## Context and problem statement

The stream drains read child output and enforce execution deadlines on the
run's asyncio event loop. Their current echo path calls the destination's
``write`` and ``flush`` methods synchronously. A slow or stuck destination can
therefore stop the same loop from reading either pipe, noticing a timeout, or
responding to cancellation.

Capture and presentation have different obligations. Captured output must
remain complete for the result, while presentation to an arbitrary Python sink
cannot be guaranteed when that sink blocks indefinitely. Delivery needs an
opt-in policy that keeps the loop responsive, limits retained output, and makes
any loss visible without changing command outcomes.

## Decision drivers

- Delivery is opt-in; omitting it preserves the existing synchronous path.
- Capture remains complete and command exit codes remain unchanged on every
  delivery path.
- Queue capacity is a byte bound, accounted using each original byte chunk's
  length before text decoding.
- Saturation drops the incoming presentation payload and reports the loss;
  it never blocks the loop or spools without a bound.
- One shared FIFO preserves the loop's observed ordering across stdout and
  stderr, including output collected from pipelines.
- Shutdown waits for a bounded drain grace and reports output that is still
  queued or in flight when that grace expires.
- The run owns one daemon worker and the right to write to its resolved
  destinations. It does not own or close caller-provided destinations.
- Diagnostics expose only the stream, a closed category, and a byte count.
  Payloads and exception text stay private.

## Considered options

1. **Keep writes synchronous.** This preserves today's behaviour but leaves a
   blocked writer on the event loop that must enforce the deadline.
2. **Run a second asyncio task on the same loop.** It still cannot advance
   while a synchronous ``write`` or ``flush`` is executing.
3. **Apply backpressure when the queue fills.** Waiting for capacity on the
   loop would recreate the read and cancellation stall this mode is intended to
   prevent.
4. **Spool output without a bound.** A destination that stops consuming can
   retain memory or disk without limit, moving the failure rather than bounding
   it.
5. **Require a standalone sink adapter.** Each consumer would have to
   reimplement capacity, ordering, shutdown, and diagnostic ownership, and
   adapters would not share the run's stdout/stderr ordering.
6. **Use one run-owned bounded worker.** This keeps the policy with command
   execution, gives both streams one queue, and can abandon a stuck writer
   without holding up the event loop. (Chosen.)

## Decision outcome

Add an opt-in ``BoundedDelivery`` configuration to ``RunOutputOptions``. Its
``max_queued_bytes`` is a strictly positive integer. The optional
``drain_grace_s`` override is a finite, non-negative number of seconds; the
default is 0.25 seconds. ``delivery=None`` keeps the synchronous writer and all
of its existing error behaviour.

Each enabled run starts one daemon thread named ``cuprum-output-delivery``. The
worker receives the stdout and stderr destinations after presentation-sink
resolution, then consumes one shared FIFO. A queued item carries its stream
identity, prepared text or bytes, a flush marker, the original byte count, and
the run-local diagnostics target. The worker completes each item's write and
flush before taking the next item, so stream order is the order in which the
event loop enqueued chunks. This is the loop's observed order; it does not
claim to reconstruct kernel-level timestamps between independent pipes.

Command and pipeline setup use one private worker constructor for the common
stdout/stderr mapping and queue capacity. Each caller resolves its own sinks,
handles the disabled-delivery case, and retains cleanup ownership if later
setup fails. The constructor does not resolve sinks or own their lifecycle.

The queue's retained payload bytes never exceed ``max_queued_bytes``. The
active write has already left the queue and is tracked separately, so at most
one in-flight payload may exist in addition to the configured queued-byte
bound. Byte accounting uses the source chunk length before text decoding, so
encoding expansion cannot evade the bound. If an enqueue would exceed the
bound, the worker rejects that incoming presentation payload. Capture and line
observation continue. Cuprum adds one ``RelayFallback`` and emits one
``EchoEvent`` with category ``DROPPED`` and the source-byte count for each
rejected chunk.

At teardown a run-owned daemon thread calls the worker's close operation, so a
saturated event-loop default executor cannot postpone the start of its drain
grace. The worker drains and joins for at most the configured grace. A write or
flush exception disables that worker's later delivery and becomes a loop-side
``DELIVERY_ERROR`` record for the affected stream. Any queued output skipped
after that failure, or still queued or in flight when the drain grace expires,
is reported as ``UNDELIVERED`` with the unconfirmed byte count grouped by
stream. These diagnostics use the existing ``RelayFallback`` and ``EchoEvent``
shapes; capture and process outcomes do not change.

If a writer remains blocked after the grace, Cuprum abandons its daemon thread.
The thread may remain alive until the destination returns. Python provides no
supported way to forcibly stop a running thread, so Cuprum does not promise to
kill it. A run with a presentation sink shares a per-run lock between delivery
writes and session close. Bounded session finalization runs on a daemon thread
and waits behind an active delivery write, so a closing frame cannot race that
write or precede it. The run waits only for the configured grace. If the active
write never returns, the session finalizer remains blocked and terminal framing
may remain unfinished. The run does not register an ``atexit`` join. A
caller-provided plain destination remains caller-owned and must stay valid for
any abandoned write or session finalizer that later returns.

The native pipeline-pump executor's unbounded hand-off rationale does not apply
here. A native pump sits between two pipeline stages: a later hop must drain
its downstream pipe before that pump can finish, so queueing pumps can deadlock
the pipeline. The delivery worker consumes output that has already been read
from the child pipes and has no cross-hop pipe dependency. Bounding and
dropping its presentation queue cannot prevent another stage from consuming a
pipe.

Python documents that daemon threads may be stopped abruptly at interpreter
shutdown and that timed ``Thread.join`` must be followed by ``is_alive`` to
determine whether the worker remains active. It also provides no operation to
forcibly stop a running thread. See the
[Python 3.13 threading documentation](https://docs.python.org/3.13/library/threading.html).

## Consequences

### Positive

- A blocked output destination no longer prevents the run loop from consuming
  child pipes, observing deadlines, or completing cancellation cleanup.
- Queue saturation has a deterministic, bounded cost and a categorical record.
- Both streams share one ordering and shutdown policy for commands and
  pipelines.
- Existing callers retain their synchronous semantics unless they select the
  new mode.

### Negative

- Opting in accepts that presentation may be dropped under saturation or
  abandoned after a worker failure or shutdown grace expiry.
- The configured byte bound excludes one in-flight payload and Python queue
  metadata; it bounds queued payload bytes rather than total process memory.
- A blocked destination can leave a daemon thread alive beyond the run's
  lifetime, and that thread may still use the caller's destination if the
  blocking operation later returns.
- A bounded presentation session can remain open if its active destination
  write never returns; its finalizer stays ordered behind that write rather
  than racing it.
- The bounded mode cannot preserve synchronous exception propagation from
  destination ``write`` and ``flush`` methods; it reports those failures
  categorically instead.

## Assumptions

- Each echo enqueue occurs on the run's event loop; the worker is the only
  thread that writes queued output.
- Callers choosing bounded delivery prefer deadline and cancellation progress
  over guaranteed presentation of every output byte.
- The default drain grace is 0.25 seconds; callers with a known destination
  latency may select a different finite grace.
