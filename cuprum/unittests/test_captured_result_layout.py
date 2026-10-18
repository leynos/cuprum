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

    assert c.CapturedResult is sh.CapturedResult
    assert "CapturedResult" in c.__all__
    assert "CapturedResult" in sh.__all__

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
    ]
    sentinel = c.RelayFallback(
        stream=c.EchoStream.STDOUT,
        error_category=c.EchoErrorCategory.UNICODE_ENCODE,
    )
    result = sh.CapturedResult(c.ECHO, (), 0, 1, "out", "err", (sentinel,))
    assert result.relay_fallbacks == (sentinel,)
    assert not {"stdout", "stderr"}.intersection(sh.CapturedResult.__slots__)
    assert not hasattr(result, "__dict__")


def test_captured_result_type_hints_resolve() -> None:
    """The narrowed stream annotations remain visible at runtime."""
    from cuprum import sh

    assert typ.get_type_hints(sh.CapturedResult)["stdout"] is str
    assert typ.get_type_hints(sh.CapturedResult)["stderr"] is str
    assert (
        typ.get_type_hints(sh.CommandResult.require_captured)["return"]
        is sh.CapturedResult
    )
