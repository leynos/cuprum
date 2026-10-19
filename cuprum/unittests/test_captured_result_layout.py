"""Tests for the public layout and construction of captured text results."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

import cuprum as c

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@pytest.mark.parametrize(
    ("stream", "invalid_value"),
    [
        ("stdout", None),
        ("stderr", None),
        ("stdout", b"out"),
        ("stderr", b"err"),
    ],
)
def test_captured_result_rejects_invalid_streams(
    stream: str,
    invalid_value: object,
) -> None:
    """A captured result requires decoded text for both streams."""
    from cuprum import sh

    invalid_stdout = invalid_value if stream == "stdout" else "out"
    invalid_stderr = invalid_value if stream == "stderr" else "err"
    constructor = typ.cast("cabc.Callable[..., sh.CapturedResult]", sh.CapturedResult)
    with pytest.raises(TypeError, match=stream):
        constructor(
            c.ECHO,
            (),
            0,
            1,
            invalid_stdout,
            invalid_stderr,
        )

    result = sh.CapturedResult(c.ECHO, (), 0, 1, "out", "err")

    with pytest.raises(TypeError, match=stream):
        dc.replace(result, **{stream: invalid_value})


def test_captured_result_preserves_the_command_result_layout() -> None:
    """The narrowed subclass keeps the command result's public layout."""
    from cuprum import sh

    assert c.CapturedResult is sh.CapturedResult, (
        "the top-level and shell packages must export the same CapturedResult"
    )
    assert "CapturedResult" in c.__all__, (
        "CapturedResult must be listed in the top-level package __all__"
    )
    assert "CapturedResult" in sh.__all__, (
        "CapturedResult must be listed in the shell package __all__"
    )

    positional = [
        field.name for field in dc.fields(sh.CapturedResult) if not field.kw_only
    ]
    assert positional == [
        "program",
        "argv",
        "exit_code",
        "pid",
        "stdout",
        "stderr",
        "relay_fallbacks",
    ], "CapturedResult must preserve CommandResult's seven positional fields"
    sentinel = c.RelayFallback(
        stream=c.EchoStream.STDOUT,
        error_category=c.EchoErrorCategory.UNICODE_ENCODE,
    )
    result = sh.CapturedResult(c.ECHO, (), 0, 1, "out", "err", (sentinel,))
    assert result.relay_fallbacks == (sentinel,), (
        "the seventh positional argument must bind to relay_fallbacks"
    )
    assert not {"stdout", "stderr"}.intersection(sh.CapturedResult.__slots__), (
        "narrowed stream fields must not allocate duplicate subclass slots"
    )
    assert not hasattr(result, "__dict__"), (
        "CapturedResult instances must remain slotted"
    )


def test_captured_result_type_hints_resolve() -> None:
    """The narrowed stream annotations remain visible at runtime."""
    from cuprum import sh

    assert typ.get_type_hints(sh.CapturedResult)["stdout"] is str, (
        "CapturedResult.stdout must resolve to str at runtime"
    )
    assert typ.get_type_hints(sh.CapturedResult)["stderr"] is str, (
        "CapturedResult.stderr must resolve to str at runtime"
    )
    assert (
        typ.get_type_hints(sh.CommandResult.require_captured)["return"]
        is sh.CapturedResult
    ), "require_captured must resolve to CapturedResult at runtime"
