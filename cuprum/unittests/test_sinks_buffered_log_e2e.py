"""Byte-level ordering for buffered custom GitHub Actions logs.

A caller can hand the sink any text destination. When that destination is a
non-line-buffered wrapper, framing written to the text layer and child bytes
echoed through ``.buffer`` share one underlying byte stream. These tests pin
the order the runner reads: the protective frame must precede child output, and
the terminal frame must follow it.
"""

from __future__ import annotations

import typing as typ

from cuprum import ScopeConfig, scoped, sh
from cuprum._sink_lifecycle import _run_label
from cuprum.sh import RunOutputOptions
from cuprum.sinks import GitHubActionsSink
from tests.helpers.catalogue import python_catalogue
from tests.helpers.github_actions_frames import (
    buffered_log,
    expected_frame,
    stop_token,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import io

    from cuprum.program import Program

_LEASE_PREFIX = b"::stop-commands::"


def _forced_sink(log: io.TextIOWrapper) -> GitHubActionsSink:
    """Build a sink over the wrapper, active outside GitHub Actions."""
    return GitHubActionsSink(log, force=True)


def _run_echoed[T](
    program: Program,
    invoke: cabc.Callable[[io.TextIOWrapper], T],
) -> tuple[T, bytes]:
    """Run one command group with echo routed to a buffered forced sink."""
    log, raw = buffered_log()
    with scoped(ScopeConfig(allowlist=frozenset([program]))):
        result = invoke(log)
    return result, raw.getvalue()


def _assert_payloads_inside_lease(value: bytes, *payloads: bytes) -> None:
    """Assert each payload sits between the stop frame and the lease release."""
    token = stop_token(value)
    opened = value.index(b"::group::")
    stop = value.index(_LEASE_PREFIX + token)
    release = value.index(b"::" + token + b"::\n")
    assert opened < stop < release, "the stop frame must open the lease"
    for payload in payloads:
        index = value.index(payload)
        assert stop < index < release, (
            f"the {payload!r} echo must sit inside the lease; "
            f"found it at {index} outside ({stop}, {release})"
        )


def test_buffered_custom_log_orders_frames_before_binary_echo() -> None:
    """A command's binary payload lands after the stop frame, capture intact."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    payload = b"\xff\xfe child output\n"
    command = python(
        "-c",
        "import sys; "
        "sys.stdout.buffer.write(b'\\xff\\xfe child output\\n'); "
        "sys.stdout.buffer.flush()",
    )

    result, value = _run_echoed(
        python_program,
        lambda log: command.run_sync(
            output=RunOutputOptions(echo=True, sink=_forced_sink(log)),
        ),
    )

    expected = expected_frame(_run_label(command), stop_token(value), payload)
    assert result.ok is True, f"the framed run must still succeed; got {result!r}"
    assert result.stdout == "\ufffd\ufffd child output\n", (
        f"capture must keep decoding the child's bytes for display; "
        f"got {result.stdout!r}"
    )
    assert value == expected, (
        f"framing and binary echo must share one ordered byte stream; "
        f"got {value!r}, expected {expected!r}"
    )


def test_buffered_custom_log_frames_both_binary_streams() -> None:
    """Each stream's payload sits between the stop frame and the lease release.

    Both echo paths write through the same buffer, so the exact interleaving of
    stdout and stderr is not part of the contract; their containment inside the
    lease is.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    stdout_payload = b"\xff out\n"
    stderr_payload = b"\x80 err\n"
    command = python(
        "-c",
        "import sys; "
        "sys.stdout.buffer.write(b'\\xff out\\n'); "
        "sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'\\x80 err\\n'); "
        "sys.stderr.buffer.flush()",
    )

    result, value = _run_echoed(
        python_program,
        lambda log: command.run_sync(
            output=RunOutputOptions(echo=True, sink=_forced_sink(log)),
        ),
    )

    assert result.ok is True, f"the framed run must still succeed; got {result!r}"
    _assert_payloads_inside_lease(value, stdout_payload, stderr_payload)
    assert value.endswith(b"::endgroup::\n"), (
        f"the group must close after the lease release; got {value!r}"
    )


def test_buffered_custom_log_orders_pipeline_frames_around_final_payload() -> None:
    """A pipeline opens one group and frames only its final stage's payload."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python(
        "-c",
        "import sys; sys.stdout.buffer.write(b'\\xff first\\n'); "
        "sys.stdout.buffer.flush()",
    )
    consumer = python(
        "-c",
        "import sys; sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(b'\\x80 final\\n'); "
        "sys.stdout.buffer.flush()",
    )
    payload = b"\x80 final\n"

    result, value = _run_echoed(
        python_program,
        lambda log: (producer | consumer).run_sync(
            output=RunOutputOptions(echo=True, sink=_forced_sink(log)),
        ),
    )

    expected = expected_frame("pipeline", stop_token(value), payload)
    assert result.ok is True, f"the framed pipeline must succeed; got {result!r}"
    assert result.stages[-1].stdout == "\ufffd final\n", (
        f"the final stage's capture must be unchanged; got {result.stages[-1].stdout!r}"
    )
    assert value == expected, (
        f"the pipeline's framing must bracket its final payload exactly; "
        f"got {value!r}, expected {expected!r}"
    )
