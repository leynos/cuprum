# Architectural decision record (ADR) 028: Transfer a detached launch's lifetime explicitly

## Status

Accepted on 2026-10-30.

## Date

2026-10-30.

## Context and problem statement

Every Cuprum execution entry point keeps its child. `run()` waits for it,
`start()` hands back a `RunningCommand` that settles it, and a pipeline owns
its stages until the last one exits. Adopters migrating from `subprocess.Popen`
have one pattern Cuprum cannot express: launching a helper that must outlive
the process and event loop that started it. The motivating consumer starts a
telemetry sampler with `stdin`, `stdout`, and `stderr` on `DEVNULL` and
`start_new_session=True`, records the sampler's PID, and exits; a later
workflow step reads that PID and stops it.

Using a managed entry point for this is wrong in both directions. A run that is
waited for blocks until the helper exits, which is the opposite of the
requirement. Dropping a `RunningCommand`, or leaving its `async with` block, is
a cancellation, not a transfer: the handle stops the child it owns. Reaching
`subprocess.Popen` directly restores the problem the adopter is trying to leave
— no catalogue, no allowlist, no argv contract, and no typed environment policy.

The question is therefore not whether Cuprum can spawn a disconnected child. It
is where lifetime transfer becomes explicit, so that a reader of a call site
can tell whether Cuprum or the caller owns the process that call produced.

## Decision drivers

- An adopter must be able to express "start this and forget it" through the
  public API, with catalogue identity, allowlist enforcement, executable
  selection, argv boundaries, environment policy, and working directory
  unchanged.
- Ownership must be visible at the call site. No flag, no dropped handle, and
  no omitted context manager may silently change who owns a child.
- The launch must return the PID of the process that actually runs, and must
  distinguish a successful start from application readiness.
- The child must hold nothing the launcher has to service: no pipe, descriptor,
  stream consumer, or runtime registration, and no dependence on the launcher's
  event loop staying alive.
- A failed or cancelled launch that has not transferred ownership must not
  leave an unmanaged child behind.
- The managed defaults must not change.

## Options considered

1. **Add `detach=True` to `run()` and `start()`.** Rejected. Capture, echo,
   timeouts, and process-group ownership are meaningless for a child the caller
   will never observe, so most of the call's options would silently do nothing.
   A reader would also have to reach the end of the argument list to learn
   which ownership contract applies.

2. **Transfer ownership when a `RunningCommand` is dropped or its `async with`
   block is left.** Rejected. Both are cancellation paths today. Making them
   transfer lifetime would mean a forgotten handle leaks a process instead of
   stopping one, and the same expression would mean opposite things at
   different call sites.

3. **Return a `RunningCommand` that is never settled.** Rejected. The handle
   promises settlement, keeps transports and consumers registered, and needs a
   live event loop. A handle that cannot answer `wait()` is a worse abstraction
   than a receipt that states what it is.

4. **A separate entry point returning an immutable receipt.** Chosen.
   `SafeCmd.launch_detached()` and `launch_detached_sync()` perform the launch
   and return a `DetachedLaunch` carrying the worker's PID, catalogued program,
   and executed file. Nothing about the returned object can stop, wait for, or
   inspect the worker, so the receipt cannot be mistaken for a handle.

## Decision outcome

The entry points are contributed by `_DetachedLaunchEntryPoints` in
`cuprum/_detached_launch.py`, a `__slots__ = ()` mixin on `SafeCmd` beside
`_OwnedStart` and the byte entry points. The launch resolves the same inputs a
managed run would — `_enforce_allowlist`, `_resolve_executable_for`, and
`_bound_executable` — and then spawns through `_spawn_detached` in
`cuprum/_detached_spawn.py`.

`_spawn_detached` uses `_DetachedPopen`, a `subprocess.Popen` subclass whose
only override is a no-op `__del__`. Spawning, the error-pipe report that turns
a failed exec into an exception, and parent-side descriptor close stay
`Popen`'s. The finalizer is removed because `Popen.__del__` reports a
still-running child as a `ResourceWarning` and enrols the object in
`subprocess._active`, which waits for the child at interpreter shutdown. Both
behaviours assume ownership that, by then, the launch has given away.

The child is started with `stdin`, `stdout`, and `stderr` on
`subprocess.DEVNULL`, `close_fds=True`, and the `start_new_session` keyword
returned by `_ownership_spawn_kwargs(ProcessGroupPolicy.OWN_GROUP)`. That
keyword is read rather than defaulted so the spawn cannot request isolation the
platform gate would refuse: the same call raises `ValueError` where POSIX
process groups do not exist, before any child is created. On POSIX the worker
leads a new session and process group, so its session and group identifiers
both equal its PID and a caller can stop it with `os.killpg`.

Ownership transfers when the receipt is returned, and the implementation treats
that as one indivisible step. The synchronous launch has no suspension point at
all. The asynchronous launch calls `_launch_detached` first — a plain
synchronous function that never suspends — and then yields once with
`await asyncio.sleep(0)` before returning the receipt. A cancellation requested
while the launch was running becomes deliverable at that yield; if it lands
there, the caller never sees the receipt, so cuprum still owns the worker and
kills its group with `_abandon_detached` before re-raising. The same
abandonment covers a failure while the receipt is being constructed. The caller
therefore either holds a receipt or has no child, and can never hold a receipt
for a worker cuprum has already stopped.

After the transfer Cuprum keeps nothing: no process object, no transport, no
stream consumer, no task, and no registration. The process object is dropped
after its PID is copied into the receipt, and the no-op finalizer makes that
drop a plain release. Nothing requires an event loop to stay alive, so a
synchronous launcher can exit immediately and the worker is re-parented to the
init process.

## Consequences

### Positive

- Adopters can replace a documented `subprocess.Popen` detach with a public API
  call without losing catalogue identity, allowlist enforcement, argv
  boundaries, environment policy, or working-directory handling.
- Detachment is one explicit call, so ownership is legible at every run site
  and no existing default changes.
- The receipt names the worker rather than an intermediate launcher: it is the
  PID `Popen` reported for the executed child.
- The synchronous and asynchronous spellings share one launch core, and the
  executable fallback rule is shared with the managed spawn through
  `_bound_executable`, so the two paths cannot drift on what runs.

### Negative

- The feature is POSIX-first. A Windows launcher receives `ValueError` rather
  than a weaker emulation.
- A receipt says the `exec` succeeded, not that the worker is ready. A worker
  that rejects its arguments or exits immediately still produces one; readiness
  remains the caller's contract with the worker.
- Termination and reaping pass to the caller. While the launcher lives, a
  worker that exits remains a zombie until the launcher exits, because reaping
  it would be the ownership the launch just gave away. A long-lived launcher
  that detaches workers must reap them itself.
- A PID identifies a process only while that process is alive and unreaped.
  Once the worker is reaped the identifier may be reused, so a stored receipt
  must not be used to signal a process the caller cannot first confirm is the
  original.
- Survival of the launcher is not survival of the environment. Job cleanup,
  container shutdown, and external termination still end the worker.

## Assumptions

- A launcher that detaches a worker is short-lived, so the zombie it leaves
  behind is reaped by the init process when the launcher exits.
- The caller that later stops the worker is the same party that received the
  receipt, or has been told the PID and the session-leadership contract.
- `subprocess.Popen` keeps its exec-failure report and its parent-side
  descriptor handling as the documented contract of the class; the subclass
  overrides only the finalizer.
