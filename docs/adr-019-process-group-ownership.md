# Architectural decision record (ADR) 019: Opt-in process-group ownership

## Status

Accepted on 2026-10-02. Cuprum adds `ProcessGroupPolicy` to `ExecutionContext`:
`INHERIT` (the unchanged default) tears down only the direct child, while
`OWN_GROUP` spawns the child as its own session and process-group leader on
POSIX and tears the whole group down through `os.killpg`. `OWN_GROUP` is
rejected with a clear error on Windows.

## Date

2026-10-02.

## Context and problem statement

Cuprum terminates the processes it spawns — the two-phase `SIGTERM`, grace,
`SIGKILL` escalation, and the cancellation-safe shielded drain are covered by
existing tests — but it terminates only the *direct child*. When that child
spawns descendants, the descendants are in the parent's process group, are not
signalled by `process.terminate()`, and can hold inherited pipe descriptors
open after their parent is reaped.

That matters most for exactly the case the timeout machinery exists to serve. A
`make` recipe, a shell script, or a test runner that has wedged is usually
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

This gives containment without an opt-in, but it changes the observable process
topology for every existing caller. A child that wanted the parent's terminal,
session, or process group — an interactive tool, a job-control shell, a process
that a caller already signals by group — silently stops receiving group signals
at the caller's own `killpg`. Under `os.setpgrp` or an interactive session, a
group signal the caller sends to its *own* group would no longer reach the
child. It also changes the meaning of a caller that has already established its
own group and expects the child to join it.

### Option B: contain descendants through a supervisor or a cgroup

Make Cuprum a process supervisor: run children under a supervision process, a
control-group, or a Job Object on every platform.

This is the only approach that can contain a descendant that calls `setsid()`,
and it is what a container runtime, a systemd unit, or `systemd-run --scope`
already provides. It is also a different product: it adds a long-lived
supervisor to a library that today spawns exactly the process the caller asked
for, needs privileges (or platform-specific orchestration) to establish control
groups, and cannot be offered uniformly across the platforms Cuprum supports. A
caller that needs this guarantee should be given the vocabulary to say so, not
have it silently imposed.

### Option C: an opt-in policy, implemented with the platform's own primitive

Add `ProcessGroupPolicy` with `INHERIT` and `OWN_GROUP`; spawn `OWN_GROUP`
children with `start_new_session=True`; signal the child's own process group
with `os.killpg` during teardown; raise a clear error for `OWN_GROUP` on
Windows, where the POSIX process group does not exist.

This keeps the default unchanged, gives callers who ask for containment the
strongest guarantee POSIX offers without a supervisor, and keeps the
implementation inside the existing lifecycle: `_spawn_subprocess` and
`_spawn_pipeline_stages` gain one spawn keyword, and
`_terminate_process_with_wait` signals a group instead of a process when the
run owns one.

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
stage. `Pipeline` reads its stages' policy from the same `ExecutionContext`, and
`SafeCmd.lines()` inherits it through the same spawn helper.

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
child. Callers needing containment for hostile or session-detaching descendants
need a supervisor, a control group, or a container.

## Goals and non-goals

### Goals

- One opt-in line
  (`ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP)`) opts a
  command or a pipeline into group ownership.
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
  group, `os.killpg` signals every member, including descendants the caller did
  not spawn through Cuprum but which joined the group. This is the intended
  semantics — it is what terminates a wedged grandchild — but callers whose
  children deliberately join the caller's own group should not opt in.
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
  future spawn path that forgets it loses the guarantee silently. The per-stage
  ownership recorded at spawn is the mitigation: teardown reads the ownership
  the spawn recorded rather than re-deriving it from the policy.
- Windows callers cannot request the guarantee at all until a Job Object
  implementation exists.

## Addendum (2026-10-03): settlement for an owning run is the group, not the child

The decision outcome above recorded that `_terminate_process_with_wait` kept its
`is_done()` short-circuit unchanged. That was wrong for an owning run, and the
implementation now amends it. This addendum records the correction; the
accepted outcome text stands as written, and an earlier section that reads as
current should be read together with this one.

An inherited-group run targets the direct child alone, so `is_done()` is
exactly the right question and the short-circuit stays for `INHERIT`: a
completed child is settlement, byte for byte as before. An owning run's target
is the group the child leads, and that group outlives its leader. When the
direct child exits — and is even reaped — while a descendant stays in the group,
`is_done()` is already true, so the old short-circuit returned without
signalling anything and the descendant survived despite `OWN_GROUP`. It also
kept the run's inherited pipes open, which is the failure the option exists to
prevent, so the policy was delivering no containment in precisely the case its
first line of documentation promised.

Settlement for an owning run is therefore decided by a null signal to the
group, not by the direct child's waiter. The kernel holds a group addressable
for as long as it has a member, and the group's name is the leader's pid, so a
name that still answers cannot have been recycled onto an unrelated group while
this one lives. The probe is safe and decisive in the same breath: it never
signals a group the run does not lead, and it knows what `is_done()` cannot —
whether a descendant is still inside.

The decision drivers and non-goals above are unchanged. In particular, "do not
claim containment that cannot be delivered" still governs, and the correction
moves the implementation towards it rather than away: what the option promises
is now what teardown performs. The bounds already recorded still bound it — a
descendant that leaves the group with `setsid()` or `setpgid()` remains out of
reach, as do ancestors and siblings. Teardown of a child that never had a pid
falls back to the direct route, matching where `_signal_child` would signal, so
there is no group name to probe and nothing to probe it with.

## Addendum (2026-10-11): settlement is decided in `_settlement_of`

The addendum above names `_terminate_process_with_wait` as the place the
short-circuit was corrected, and that is the function that consults the rule.
The rule itself is stated in `_settlement_of`, which the correction introduced
in place of the bare `is_done()` the entry point evaluated before it.
`_terminate_process_with_wait` no longer holds a settlement condition of its
own: it reads its target's settlement from the helper, so there is no
`is_done()` call left in that function for a later reader to inspect.

The helper answers with the probe's three-way `_GroupProbe` rather than a
boolean, so that an unsignalable group is distinguishable from an emptied one,
and `_report_settlement` is its companion: the query reports what it found and
the command boundary decides and logs. Keeping the two apart is what leaves the
query assertable without a log capture.

That single definition is the point rather than an implementation detail, and
the kernel argument is why it can be stated once. The question "is this
teardown target settled?" has one answer per run, and the answer turns on the
same fact wherever it is asked — a group stays addressable for as long as it
holds a member, so a name that still answers cannot have been recycled onto an
unrelated group. The probe is asked only where `_signal_child` would signal the
group: a child with no pid has no group name to ask about, and settlement for
that child is the same question the inherited route asks. A child with a pid
that the run does not own keeps the `is_done()` short-circuit byte for byte.

Every teardown route reaches this one helper. The pipeline cleanup path goes
through `_terminate_all_shielded` and `_terminate_process` to
`_terminate_process_with_wait`, the single caller of `_settlement_of`, so a
pipeline stage and a single command are settled by the same code.

The mandatory `owns_group` keyword on the wait helper, and the explicit
`owns_group=False` at every direct caller, are part of the same boundary: with
the rule in one place, the flag that selects between its two routes is the
thing a caller can get wrong, so it may not be omitted silently.

## Addendum (2026-10-11): the identifier guarantee is one-directional

Both addenda above justify the liveness probe with a kernel property, and state
it as a two-way guarantee: "a name that still answers cannot have been recycled
onto an unrelated group while this one lives". The property itself is sound —
the kernel does hold a group addressable for as long as it has a member — but
the guarantee drawn from it is stronger than the property supports, and the
difference matters to anyone reasoning about worst-case safety here.

What the property gives is one direction. A probe that still answers proves the
name is still this run's group: a group that has emptied would not answer, so
an answer is conclusive. A probe that has *stopped* answering proves only that
the group has emptied, and says nothing about what the kernel did with the
identifier afterwards — once the group is gone, its identifier is an ordinary
free slot that may be recycled like any other. The two addenda's phrasing reads
as though the name were reserved for this run's lifetime and could not be
reused even after the group ended, and that is not what the kernel promises.

There is also a window the probe cannot close, which the earlier text did not
account for. The probe and the signal it guards are separate syscalls, so a
group that empties between them leaves the signal to be sent under a name the
kernel may already have recycled. Closing that window would need a
lifetime-safe handle on the group — the analogue of `pidfd` for a process. No
such primitive exists: `os.pidfd_open` and `signal.pidfd_send_signal` are
per-pid only, and no process-group equivalent is available on Linux.
"Lifetime-safe signalling where supported" therefore has nothing to select on
this platform.

The residual risk is real but small, and it is the one already familiar from
`Popen`: the same check-then-signal gap between a liveness test and an
`os.kill`, which CPython documents as bpo-38630. What bounds it here is the
kernel's own recycling rate — an identifier must be freed and handed to a new
process before the gap can be exploited, and identifier space on current
kernels makes that improbable within a teardown window.

Nothing about the decision changes. The probe stays, because a two-way
guarantee is not needed for the deduction it supports: settlement of an owning
run still cannot be decided by the direct child's waiter, a live probe still
answers the question that matters, and the alternative that the first addendum
rejected — reverting to `is_done()` alone — would leak descendants exactly as
before. This addendum corrects the strength of a stated guarantee, not the
design that rests on it. The corrected statement lives in
`cuprum/_process_signal.py` and in the developers' guide, so the code and the
guide no longer claim more than the kernel provides.

## Addendum (2026-10-11): the guarantee is a veto, not a licence

The addendum above records that only one direction of the identifier guarantee
holds, but it draws the wrong conclusion from it: it states that "a probe that
still answers proves the name is still this run's group: a group that has
emptied would not answer, so an answer is conclusive". That inference does not
follow, and the earlier addenda still read as though a live probe licensed the
signal that follows it. The kernel holds a number for as long as *a* group
holds it, not for as long as a *particular* group holds it, so an answer
attributes nothing. Stated plainly, the probe admits exactly one sound
inference:

- A probe that has stopped answering is **conclusive**: no group holds the
  number, so it certainly is not this run's group, and no signal may be sent.
- A probe that still answers is **necessary but not sufficient**: it proves
  only that *some* group holds the number. This run's group could have emptied
  and the kernel could have recycled the number **before** the probe ran, in
  which case the answering group is a stranger's.

So the probe is a veto and never a licence. It can forbid a signal; it can
never authorize one, because no probe can attribute a live name to this run.

That distinction is not pedantry — it is the difference between a design that
knows its residual and one that mistakes it for safety. A reading in which an
answering probe authorizes the signal would treat the reuse-before-probe window
as closed when it is open, and a future change that made the probe compulsory
while treating a positive result as a green light would look like a hardening
while widening exposure. Recording the one-way inference here is what prevents
that.

The tests in `cuprum/unittests/test_process_signal_identity.py` pin the
behaviour the direction implies. They assert the outcome rather than forbidding
it — a stranger's group is signalled when the name answers, because that is
what the kernel does with a number — and they record that the test suite cannot
adjudicate the reading itself: a teardown taking a live answer as proof of
ownership sends the same signals to the same names as one taking it as a veto
that did not fire, so no input separates them and no assertion fails under
either. The claim is enforced by this document and the module docstring of
`cuprum/_process_signal.py`, which names both reuse windows — the larger one
before the probe and the smaller one between the probe and the signal — and not
by the tests.

## Addendum (2026-10-11): a configured timeout bounds the stream collection

Owning a group made a timeout mean less than it says, and the gap was in the
half of the run that ownership introduced. The deadline was consulted only by
the exit wait, so "the child exited before the deadline" was taken as "the run
is finished". For an owning run those are different claims: a child can exit
well inside its deadline while a descendant it left behind is still holding the
run's inherited pipes. The consumers then never see end of file, and the run
waited on them indefinitely — past its own deadline, which nothing was watching
any more. The timeout measured from the run's start silently covered only its
first half.

The stream collection is now bounded by what remains of the same deadline, from
the same reference the exit wait's deadline was scheduled against. A run that
has already spent its budget has nothing left to spend here, and a run with no
configured timeout stays unbounded exactly as it was everywhere else. The
timeout therefore means what its documentation has always said it means:
wall-clock, measured from the run's start, and expiring whether it was the
child or a descendant that kept the run alive.

The second half is what expiry *does*. A deadline that merely stopped waiting
would return on time and leave the descendant holding the pipes — the leak the
option exists to prevent, arrived at by a different route. Expiry instead takes
the ordinary error route: the group is settled, which is precisely what
releases the pipes the descendant was holding, and only then does the resulting
`TimeoutError` become the internal `_SubprocessTimeoutError` and, at the
execution boundary, the public `TimeoutExpired` — the same exception a deadline
expiring during the exit wait produces. One failure contract covers both
halves, and no expiry path leaves the group unsettled.

Bounding the second half is itself not enough, and the residual gap is worth
recording because it is not visible from the shape of the scope. A deadline can
expire and still never reach its handler *inside* it. Cancelling a task that is
suspended on ``await other_task`` cancels the *inner* task and forwards the
``CancelledError`` out only once that inner task finishes, so an await that
parks indefinitely defers the very expiry meant to interrupt it. The stdin
writer is exactly such an await: a descendant that inherits the child's stdin
without reading it leaves the writer parked in ``drain``, and the cancellation
that reaches it lands inside its own ``finally`` at ``wait_closed``, which
waits for a protocol waiter a cancelled close never resolves. That was not a
theoretical hazard — it was observed with the run at ``cancelling() == 1`` and
the writer parked at ``wait_closed`` for as long as the probe ran.

Two changes close it, and the split between them is deliberate. The close
itself now has a bounded grace, so the writer always finishes promptly once
cancelled; that bound is the necessary one, because it is also what the expiry
handler's reconciliation needs when it cancels and collects the writer. The
writer await is additionally shielded, so the cancellation returns to the
boundary at once instead of after the writer has spent that grace. Measured
against a 1.0 s deadline with a parked writer, the expiry lands at 1.52 s
shielded and 2.02 s unshielded. Shielding alone is *not* a fix: with the close
left unbounded, the run still hung past a 90 s cap, because the shield only
moved the wait from the boundary into the reconciliation that awaits the writer
it cancels. The bounded close is what makes the run terminate; the shield
shortens the path to that termination.

The ``INHERIT`` policy is what makes this observable, and the deadline tests
run both policies for that reason. Under ``OWN_GROUP`` the expiry's cleanup
signals the group, which closes the pipes and releases the writer whether or
not reconciliation ever reaches it — so a run that bounded the writer await and
never settled the writer still returns on time. Under ``INHERIT`` there is no
group signal: the writer stays parked unless reconciliation cancels it, and a
cleanup that does not reach the writer hangs the run past its deadline. Only
the pair distinguishes a boundary that is merely entered from one whose cleanup
actually completes.

This does not disturb the fixed post-exit EOF window recorded under "Timeouts"
in the users' guide. That window belongs to the drain, which is reached once
the group has been settled; here the question is what bounds the gather that
precedes it. The two are complementary: the deadline decides how long the run
may wait, and the EOF window decides how long a reader may hold on once the run
has decided.

## Addendum (2026-10-11): group teardown is reported through log records, not events

Process-group teardown is observable through three diagnostic log records and
nothing else: no observe event and no metric. The adjacent teardown bound that
this feature most resembles — the bounded capture EOF grace — *does* have a
counter (`cuprum_capture_eof_grace_expired_total`), so the omission was
recorded as a decision rather than left to read as an oversight.

The reason is attribution. An observe event is emitted from a stage's
``_StageObservation``, and group teardown has none to emit through. It is
reached from ``_terminate_all_shielded``, which the pipeline fail-fast path
calls for every still-running stage at once: there is no single stage a group
event could be attributed to, and the single-command routes reach the same
helper from callers that have already abandoned the run. Threading an
observation down would mean inventing an attribution for a signal that belongs
to the run rather than to any one stage. Metrics are declined for the same
reason and one more: the useful signal here is whether a group survived a
teardown this run could not complete, and the records below already carry it
with the cardinality an operator needs.

The three records, all WARNING because each reports an abnormal outcome:

- ``cuprum.process_group_unsignalable`` — the teardown command found a group
  holding members this process may not signal, so the group was reported
  unreachable rather than settled.
- ``cuprum.process_group_settlement_timeout`` — the capped post-escalation group
  wait expired with members still outstanding.
- ``cuprum.teardown_signal_failed`` — a signal the kernel refused with an
  ``OSError`` wider than the tolerated race.

The third is new here and closes a defect that was more than a logging gap. A
teardown signal had two ways to fail to arrive and reported both as the same
``False``, meaning "nothing left to do here". ``ProcessLookupError`` is the
race both signal routes already tolerate — the target went between the probe
and the signal — and silence is correct for it. A wider ``OSError`` is a
process this run believes is running and could not signal, and reporting it as
an orderly settlement made a failed teardown indistinguishable from a
successful one: the fail-fast path reads that ``False`` as a stage that had
already settled and counts it into ``cuprum_terminated_stage_count``. A refusal
was therefore trended as a clean outcome. The two signal sites now share one
wrapper that separates the arms, so the tolerated race stays quiet and a
refusal is recorded once, naming the signal it could not deliver.

## Addendum (2026-10-11): correction to the propagation account above

The addendum above is left in place, because accepted ADR text is append-only.
Two of its claims are wrong and are corrected here rather than edited.

**The count claim.** It says the fail-fast path "reads that ``False`` as a
stage that had already settled and counts it into
``cuprum_terminated_stage_count``", implying a refused signal inflated the
count. It does not. The fail-fast chain is ``_terminate_process_with_wait`` →
``_terminate_process_via_wait_task`` → ``_await_teardown_shielded`` →
``_terminate_pipeline_remaining_stages`` → ``_terminate_and_report``. It does
not pass through ``_terminate_all_shielded``, which returns ``None`` and
discards its outcomes; that helper is a sibling route, reached from the stream,
single-command, and spawn paths.

``_terminate_pipeline_remaining_stages`` reduces its outcomes with
``outcome is True``, so a refused signal has always contributed **zero** to the
count — the same as an absent target. The defect was therefore a **conflation**
of absence with failure, not an **inflation** of the tally: a reader could not
tell a swept-and-clean stage from one whose signal the kernel refused. The
record fixes the diagnosis. It does not, and was never going to, change the
number.

**The attribution claim.** It gives attribution as the reason no metric exists,
on the grounds that group teardown "is reached from
``_terminate_all_shielded``, which the pipeline fail-fast path calls for every
still-running stage at once", so no single stage could own a group event. The
premise is the same wrong helper, and the conclusion does not follow from it
either. The fail-fast path reaches group teardown through
``_terminate_pipeline_remaining_stages``, which carries a stage index, and its
caller holds the ``_StageObservation`` it emits
``cuprum_terminated_stage_count`` through. A per-stage or run-level group
metric is therefore constructible; it is unbuilt, not impossible. The decline
stands on the narrower ground that the three records already carry the signal
an operator needs, and that cardinality is a property of a metric's labels
rather than of how many targets a run reaches.
