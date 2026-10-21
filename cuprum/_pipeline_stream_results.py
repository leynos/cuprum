"""Await, cancel, and collect results from pipeline stream tasks.

This module owns the teardown and result-collection half of pipeline stream
handling. Once ``cuprum._pipeline_streams`` has created the capture and pump
tasks, these helpers gather their results, cancel the consumer tasks during
cleanup, and surface any unexpected pipe failures. They deliberately hold no
knowledge of how the tasks were created or which backend pumps the bytes; they
operate purely on the resulting :class:`asyncio.Task` objects.

Each consumer task settles to a whole
:class:`~cuprum._stream_drain_finish._CapturedDrain`, not to a bare payload, and
the gathering helpers report what the tasks produced rather than narrowing it.
The payload is one of three things a drain result carries — the capture record
and the spill handle travel with it — so a helper that returned only the
payload would discard a record naming a file nobody could then remove.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

from cuprum._stream_drain import (
    _CAPTURE_EOF_GRACE_S,
    _decode_consumer_result,
    _report_eof_grace_expiry,
    _settle_consumers,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum._subprocess_wait_types import (
        _DrainContext,
        _StreamConsumerTask,
    )


def _flatten_stream_tasks(
    stderr_tasks: list[_StreamConsumerTask | None],
    stdout_task: _StreamConsumerTask | None,
) -> list[_StreamConsumerTask]:
    """Collect all running stream consumer tasks for cancellation cleanup."""
    tasks = [task for task in stderr_tasks if task is not None]
    if stdout_task is not None:
        tasks.append(stdout_task)
    return tasks


async def _cancel_stream_tasks(
    stderr_tasks: list[_StreamConsumerTask | None],
    stdout_task: _StreamConsumerTask | None,
) -> None:
    """Cancel stream consumer tasks and await their completion."""
    tasks = _flatten_stream_tasks(stderr_tasks, stdout_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _bounded_payloads(
    tasks: list[_StreamConsumerTask | None],
    *,
    deadline: float | None,
) -> tuple[tuple[_CapturedDrain | None, ...], bool]:
    """Await optional capture tasks, bounded by an absolute deadline.

    Each slot holds the whole drain result its task settled to, so the payload,
    the capture record, and any spill handle stay together — which is what lets
    a later stage report how the stream was retained and close the file it
    left. A slot is ``None`` where no task was supplied, which is a stream the
    spawn never attached a consumer to.

    The bound is expressed as ``asyncio.wait``'s remaining budget rather than
    as a cancellation: a task that has already produced a drain result keeps
    it, and one still running when the budget runs out is reported as
    unfinished for the caller to settle. Observing rather than cancelling is
    what lets a caller settle readers through the same path a teardown uses —
    the repository's line-stream contract is that a delivered line stays
    delivered, and a cancel here would be a second, differently-behaved way to
    end a reader.

    A finished task that raised is re-raised as its own exception. A
    ``CancelledError`` is re-raised bare so the caller's cancellation is not
    relabelled as a deadline expiry; the expiration flag is only ever set by
    this function's own budget running out.

    Parameters
    ----------
    tasks:
        Consumer tasks to await, in stage order. ``None`` marks a stage that
        has no consumer for this stream and contributes a ``None`` slot.
    deadline:
        Absolute ``time.monotonic()`` reading the wait must not pass, or
        ``None`` for no bound at all.

    Returns
    -------
    tuple[tuple[_CapturedDrain | None, ...], bool]
        One slot per input task, in the same order, and whether every reader
        finished, so that a ``False`` means "give the rest their grace". An
        absent slot contributes no reader and never holds the flag back.
    """
    indexed = [(index, task) for index, task in enumerate(tasks) if task is not None]
    if not indexed:
        # ``asyncio.wait`` rejects an empty set. A pipeline with no consumers
        # has no unfinished reader, so it reports a finished wait rather than
        # an expiry that never happened.
        return tuple([None] * len(tasks)), True

    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    done, pending = await asyncio.wait(
        [task for _, task in indexed],
        timeout=remaining,
        return_when=asyncio.FIRST_EXCEPTION,
    )
    by_task = dict(
        zip(done, await asyncio.gather(*done, return_exceptions=True), strict=True),
    )
    payloads: list[_CapturedDrain | None] = [None] * len(tasks)
    for index, task in indexed:
        match by_task.get(task):
            case asyncio.CancelledError() as cancelled:
                # Re-raised bare so the caller sees its own cancellation
                # rather than an expiry this function never observed. The
                # arm has to come first: cancellation is a ``BaseException``,
                # so the general failure arm below would otherwise catch it.
                raise cancelled
            case BaseException() as failure:
                raise failure
            case drain:
                payloads[index] = drain
    return tuple(payloads), not pending


async def _settle_pipeline_outputs(
    payloads: cabc.Sequence[_CapturedDrain | None],
    tasks: list[_StreamConsumerTask | None],
    *,
    context: _DrainContext,
    expired: bool,
) -> tuple[tuple[_CapturedDrain | None, ...], _CapturedDrain | None]:
    """Settle every pipeline consumer and report its output.

    This is the pipeline's counterpart to
    :func:`~cuprum._stream_drain._drain_stream_consumers`. That helper is typed
    and shaped for the direct run's stdout/stderr pair, so it is not reusable
    for a run holding one stderr consumer per stage plus at most one final
    stdout consumer. What *is* reused is the part that must not be duplicated:
    the 0.25 s capture grace, the pending-reader accounting, and the
    mode-dependent fallback an absent reader owes its caller.

    Readers are always settled here, on both routes. A pipeline's post-exit
    drain has to reconcile its consumers on the success path too, and doing it
    in one place is what keeps a reader's payload from depending on which route
    ended the run: a reader that already produced a drain result keeps it
    either way, and only readers still running are cancelled.

    Parameters
    ----------
    payloads:
        What the bounded wait observed before settlement, one slot per task.
    tasks:
        The consumer tasks, stderr by stage followed by the optional final
        stdout task. ``None`` marks a stage with no consumer for that stream,
        and contributes a ``None`` slot to the stderr result.
    context:
        The drain context supplying the resolved capture policies, the
        per-stream fallbacks those policies owe, and the telemetry identity.
    expired:
        Whether the bounded wait gave up with readers still unfinished. Only
        then does a capturing drain owe the remaining readers their EOF grace;
        a wait that saw every reader finish has nothing left to wait for.

    Returns
    -------
    tuple[tuple[_CapturedDrain | None, ...], _CapturedDrain | None]
        The per-stage stderr results and the final stage's stdout result, each
        carrying the record describing how its stream was retained.
    """
    consumers = tuple(task for task in tasks if task is not None)
    if _owes_eof_grace(consumers, context, expired=expired):
        await _await_pipeline_eof_grace(consumers, context)
    settled = await _settle_consumers(
        consumers,
        discard_on_cancel=(None if context.capturing else context.discard_on_cancel),
    )
    by_task = dict(zip(consumers, settled, strict=True))
    return _pipeline_payloads(payloads, tasks, by_task, context)


def _owes_eof_grace(
    consumers: tuple[_StreamConsumerTask, ...],
    context: _DrainContext,
    *,
    expired: bool,
) -> bool:
    """Whether a capturing settlement still owes its readers the EOF grace."""
    return expired and context.capturing and bool(consumers)


def _pipeline_payloads(
    payloads: cabc.Sequence[_CapturedDrain | None],
    tasks: list[_StreamConsumerTask | None],
    by_task: cabc.Mapping[
        _StreamConsumerTask,
        _CapturedDrain | BaseException | None,
    ],
    context: _DrainContext,
) -> tuple[tuple[_CapturedDrain | None, ...], _CapturedDrain | None]:
    """Map each task onto its stage slot, decoding what settlement added.

    ``tasks`` is the stderr list followed by the final stdout task, so the last
    position carries stdout, and only when the pipeline attached that consumer.
    A ``None`` stderr slot stays ``None``: that stage has no consumer for this
    stream rather than a consumer that came up empty, which is the distinction
    the gather has always drawn.

    An observed result wins over the settled one, because it is the reader's
    own output seen before any cancellation could reach it. Settlement fills
    the remaining slots, and one it still leaves absent is a lost reader, so
    :func:`~cuprum._stream_drain._decode_consumer_result` gives it the empty
    value its own stream's policy owes — the per-stream rule the direct run
    applies, which is why the stream index is the slot's own position in the
    stderr list (stdout is never an absent slot here: ``None`` in the final
    position means the spawn attached no stdout consumer at all).

    Returns
    -------
    tuple[tuple[_CapturedDrain | None, ...], _CapturedDrain | None]
        The per-stage stderr results and the final stage's stdout result.
    """
    decoded: list[_CapturedDrain | None] = []
    for index, task in enumerate(tasks):
        observed = payloads[index]
        if task is None or observed is not None:
            decoded.append(observed)
        else:
            # A stage's stderr slot indexes the stderr policy, and the final
            # stdout slot always indexes the stdout one, whichever position it
            # occupies in this flat list.
            stream = 0 if index == len(tasks) - 1 else 1
            decoded.append(
                _decode_consumer_result(
                    by_task.get(task),
                    stream=stream,
                    context=context,
                )
            )
    return tuple(decoded[:-1]), decoded[-1]


async def _await_pipeline_eof_grace(
    consumers: tuple[_StreamConsumerTask, ...],
    context: _DrainContext,
) -> None:
    """Give a capturing pipeline's unfinished readers their bounded chance.

    The wait is composed here rather than delegated to
    ``_await_capture_eof_grace``: that helper and its ``eof_grace_waiter`` hook
    are typed over the direct run's two-element pair, so handing either of them
    a pipeline's N consumers would mean widening a type this change does not
    own or casting a lie about the tuple length. What *is* shared comes from
    :mod:`cuprum._stream_drain` — the single ``_CAPTURE_EOF_GRACE_S`` bound, the
    pending-reader accounting, and the expiry it reports through
    :func:`~cuprum._stream_drain._report_eof_grace_expiry` — so a change to the
    grace cannot reach one route and miss the other.

    The hook is deliberately not consulted. It exists so a direct-run test can
    replace a wall-clock wait with a deterministic one, and a pipeline has no
    such caller; honouring it only when the pipeline happens to hold two
    readers would make the test seam behave differently at stage counts the
    test cannot see.
    """
    await asyncio.wait(consumers, timeout=_CAPTURE_EOF_GRACE_S)
    _report_eof_grace_expiry(consumers, context)


async def _collect_pipe_results(
    pipe_tasks: list[asyncio.Task[None]],
) -> list[object]:
    """Collect pipe task results, capturing exceptions rather than raising them.

    Uses return_exceptions=True to gather all results including any exceptions
    that occurred during pipe streaming between pipeline stages.

    Returns
    -------
    list[object]
        Each pipe task's result, or the exception it raised.
    """
    return list(await asyncio.gather(*pipe_tasks, return_exceptions=True))


async def _reconcile_pipe_tasks(
    pipe_tasks: list[asyncio.Task[None]],
) -> list[object]:
    """Cancel and drain the inter-stage pumps after a pipeline deadline.

    Safe to run whether or not ``_wait_for_pipeline`` already reconciled them:
    cancelling a finished task is a no-op and gathering an already-gathered one
    returns its recorded outcome. Failures are absorbed because a
    ``TimeoutExpired`` is already propagating and a broken pump must not
    replace it.

    Returns
    -------
    list[object]
        Each pump task's result, including cancellation or failure outcomes.
    """
    for task in pipe_tasks:
        if not task.done():
            task.cancel()
    return await _collect_pipe_results(pipe_tasks)


def _surface_unexpected_pipe_failures(pipe_results: list[object]) -> None:
    """Raise non-BrokenPipe exceptions from pipe results.

    BrokenPipeError and ConnectionResetError are expected when downstream
    processes terminate early (e.g., head) and should not fail the pipeline.
    Other exceptions indicate genuine failures and must be surfaced.

    The final case matches ``BaseException`` rather than ``Exception`` so a
    cancelled pipe task cannot be dropped: ``asyncio.CancelledError`` derives
    from ``BaseException``, and ``_collect_pipe_results`` gathers with
    ``return_exceptions=True``, so a cancellation arrives as an ordinary
    element here. Matching only ``Exception`` would skip it and let the
    pipeline report success.
    """
    for result in pipe_results:
        match result:
            case BrokenPipeError() | ConnectionResetError():
                continue
            case BaseException():
                raise result
