# Architectural decision record (ADR) 019: Opt-in process-group ownership

## Status

Accepted on 2026-10-02. Cuprum adds `ProcessGroupPolicy` to
`ExecutionContext`: `INHERIT` (the unchanged default) tears down only the
direct child, while `OWN_GROUP` spawns the child as its own session and
process-group leader on POSIX and tears the whole group down through
`os.killpg`. `OWN_GROUP` is rejected with a clear error on Windows.

## Date

2026-10-02.

## Context and problem statement

Cuprum terminates the processes it spawns — the two-phase `SIGTERM`, grace,
`SIGKILL` escalation, and the cancellation-safe shielded drain are covered by
existing tests — but it terminates only the *direct child*. When that child
spawns descendants, the descendants are in the parent's process group, are not
signalled by `process.terminate()`, and can hold inherited pipe descriptors
open after their parent is reaped.

That matters most for exactly the case the timeout machinery exists to serve.
A `make` recipe, a shell script, or a test runner that has wedged is usually
waiting on a grandchild. Terminating the runner leaves the grandchild alive, a
reader waiting for EOF never sees it, and the capture drain falls back to its
bounded grace window with output still buffered in a dead process's pipe. The
result is not a leak of Cuprum's own handles — it is a run that reports a
timeout while the work it timed out continues in the background.

The guarantee Cuprum can offer here is bounded and must be stated precisely. On
POSIX, `start_new_session=True` makes the child a *session leader* and a
*process-group leader*, so teardown can signal the group with `os.killpg`. It
does **not** follow that every descendant is in that group: a descendant that
calls `setsid()` (or `setpgid()`) deliberately escapes, and no non-privileged
mechanism can contain it. Anything stronger would need a supervisor, a cgroup,
or a container, and belongs to the caller rather than to a command runner.

## Decision drivers

- Preserve the existing default exactly: callers who say nothing keep
  direct-child-only teardown, byte-for-byte.
- Make the containment promise precise enough to test: group identity is the
  child's own process group, and only that group is signalled.
- Never signal a process group the test runner belongs to. A group signal is
  only sent for a group created *by* the spawn, which the child leads.
- Reuse the existing two-phase grace, the shielded cleanup, and every existing
  teardown entry point rather than adding a second lifecycle.
- Reject `OWN_GROUP` where the platform cannot honour it, rather than accept
  the option and silently provide no containment.
- Do not claim containment that cannot be delivered. Ancestors, siblings, and
  descendants that call `setsid()` are out of scope and documented as such.

## Options considered

### Option A: change the default so every child is a group leader

Spawn every child with `start_new_session=True` and tear the group down.

This gives containment without an opt-in, but it changes the observable
process topology for every existing caller. A child that wanted the parent's
terminal, session, or process group — an interactive tool, a job-control
shell, a process that a caller already signals by group — silently stops
receiving group signals at the caller's own `killpg`. Under `os.setpgrp` or an
interactive session, a group signal the caller sends to its *own* group would
no longer reach the child. It also changes the meaning of a caller that has
already established its own group and expects the child to join it.

### Option B: contain descendants through a supervisor or a cgroup

Make Cuprum a process supervisor: run children under a supervision process, a
control-group, or a Job Object on every platform.

This is the only approach that can contain a descendant that calls `setsid()`,
and it is what a container runtime, a systemd unit, or `systemd-run --scope`
already provides. It is also a different product: it adds a long-lived
supervisor to a library that today spawns exactly the process the caller asked
for, needs privileges (or platform-specific orchestration) to establish
control groups, and cannot be offered uniformly across the platforms Cuprum
supports. A caller that needs this guarantee should be given the vocabulary to
say so, not have it silently imposed.

### Option C: an opt-in policy, implemented with the platform's own primitive

Add `ProcessGroupPolicy` with `INHERIT` and `OWN_GROUP`; spawn `OWN_GROUP`
children with `start_new_session=True`; signal the child's own process group
with `os.killpg` during teardown; raise a clear error for `OWN_GROUP` on
Windows, where the POSIX process group does not exist.

This keeps the default unchanged, gives callers who ask for containment the
strongest guarantee POSIX offers without a supervisor, and keeps the
implementation inside the existing lifecycle: `_spawn_subprocess` and
`_spawn_pipeline_stages` gain one spawn keyword, and `_terminate_process_with_wait`
signals a group instead of a process when the run owns one.

## Decision outcome / proposed direction

Option C.

`ProcessGroupPolicy` is a `StrEnum` in `cuprum.sh.execution` with two members:

- `INHERIT` (the default) — the child joins the parent's process group and
  session exactly as before. Teardown signals the direct child only, so
  direct-child-only semantics are preserved for every existing caller.
- `OWN_GROUP` — the child is spawned with `start_new_session=True`. On POSIX
  this places the child in a new session and a new process group, with the
  child as both session leader and process-group leader; its process-group
  identifier is therefore its own process identifier (`PGID == PID`), which is
  how teardown addresses the group. No `os.getpgid` lookup is required, and
  none is attempted.

The policy rides on `ExecutionContext.process_group`, alongside `cwd` and
`env_mode`, so it applies identically to single commands and to every pipeline
stage. `Pipeline` reads its stages' policy from the same `ExecutionContext`,
and `SafeCmd.lines()` inherits it through the same spawn helper.

Teardown is unchanged in structure. `_terminate_process_with_wait` keeps its
`is_done()` short-circuit, its `SIGTERM`-then-grace-then-`SIGKILL` sequence,
and its shielded, cancellation-safe wrapper; the only difference is that the
signal is delivered with `os.killpg(pgid, sig)` rather than
`process.terminate()`/`process.kill()` when the run owns its group. Both
signalling routes absorb `ProcessLookupError` and `OSError` exactly as the
direct path already does.

Windows rejects `OWN_GROUP` at spawn with a `ValueError` naming the option and
the platform. Windows has no POSIX process group; containment there is a Job
Object, which must be assigned to the process *after* it is created, in the
window between `CreateProcess` returning and the assignment landing. A child
that spawns a descendant inside that window is outside the Job Object, so the
guarantee is not the one the option promises. Rejecting the request is honest;
silently ignoring it would not be. A Windows Job Object implementation is
tracked separately, not by this decision.

The escape hatch is documented rather than hidden. A descendant that calls
`setsid()` or `setpgid()` leaves the group and is not terminated by group
teardown; so are processes that predate the spawn, and so are siblings of the
child. Callers needing containment for hostile or session-detaching
descendants need a supervisor, a control group, or a container.

## Goals and non-goals

### Goals

- One opt-in line (`ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP)`)
  opts a command or a pipeline into group ownership.
- `INHERIT` callers observe no change: same spawn arguments, same teardown
  signals, same results.
- Terminating a run whose process the caller owns terminates the whole
  process group, including a descendant that ignores `SIGTERM` and one that
  holds an inherited pipe open.
- A refusal on Windows is explicit, immediate, and names the reason.
- Group identity is derived from the spawn itself, with no lookup and no
  window in which a recycled identifier could be signalled.

### Non-goals

- Terminating ancestors, siblings, or unrelated processes. Group teardown is
  scoped to the group the run created.
- Containing a descendant that deliberately leaves the group with `setsid()`
  or `setpgid()`.
- Changing the default policy, or the teardown of any caller that does not
  opt in.
- A Windows Job Object implementation. The platform's rejection is the
  decision recorded here; the implementation is future work.

## Known risks and limitations

- **Descendants can leave the group.** `setsid()` (or `setpgid()`) removes a
  descendant from the group, and it is then outside the teardown. This is a
  limitation of the primitive, not of the implementation, and it is the reason
  the option is named "own group" rather than "contain descendants".
- **A group signal is broader than a process signal.** When a run owns its
  group, `os.killpg` signals every member, including descendants the caller
  did not spawn through Cuprum but which joined the group. This is the
  intended semantics — it is what terminates a wedged grandchild — but callers
  whose children deliberately join the caller's own group should not opt in.
- **Group teardown does not reap descendants.** Cuprum reaps the direct child
  it spawned. Descendants signalled through the group are reaped by their own
  parent, or by `init` when their parent dies first; a descendant that is
  already a zombie has no live process to signal and none is required.
- **Repeated cancellation does not weaken cleanup.** The group signal runs
  inside the existing shielded cleanup, so cancellation arriving mid-grace
  cannot skip the `SIGKILL` escalation and strand a group member.
- **Pipeline stages own one group each.** Every stage is its own group
  leader, so fail-fast, timeout, and partial-spawn teardown all address their
  stages' groups; a stage that exits does not affect a sibling's group.

## Consequences

### Positive

- A timed-out or cancelled run can terminate the grandchildren it started,
  so inherited pipes reach end-of-file and the drain is not held open by a
  process the run no longer controls.
- The guarantee is explicit, testable, and opt-in; no existing caller is
  affected.
- Teardown keeps one implementation and one set of cancellation guarantees,
  because the policy changes which signal is sent rather than which lifecycle
  runs.

### Negative

- Callers opting in must reason about group membership: a child that is
  deliberately placed in the caller's own group by the caller's own
  orchestration should not be run with `OWN_GROUP`.
- The policy must be threaded through every spawn and teardown site, so a
  future spawn path that forgets it loses the guarantee silently. The
  per-stage ownership recorded at spawn is the mitigation: teardown reads the
  ownership the spawn recorded rather than re-deriving it from the policy.
- Windows callers cannot request the guarantee at all until a Job Object
  implementation exists.
