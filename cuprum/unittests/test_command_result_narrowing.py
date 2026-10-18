"""Tests for checked narrowing from command results to captured text."""

from __future__ import annotations

import typing as typ

import pytest

import cuprum as c


def test_command_result_require_captured_copies_every_public_field() -> None:
    """Narrowing a base result preserves execution and measurement fields."""
    fallback = c.RelayFallback(
        stream=c.EchoStream.STDOUT,
        error_category=c.EchoErrorCategory.UNICODE_ENCODE,
    )
    result = c.CommandResult(
        c.ECHO,
        ("argument",),
        7,
        4242,
        "out",
        "err",
        started_at=12.5,
        duration=0.75,
        max_rss_bytes=1024,
        user_cpu_seconds=0.25,
        system_cpu_seconds=0.125,
        resolved_path="/bin/echo",
        relay_fallbacks=(fallback,),
    )

    captured = result.require_captured()

    assert type(captured) is c.CapturedResult, (
        f"narrowing must return CapturedResult, got {type(captured).__name__}"
    )
    assert captured.program is result.program, "narrowing must preserve the program"
    assert captured.argv == result.argv, "narrowing must preserve command arguments"
    assert captured.exit_code == result.exit_code, "narrowing must preserve exit status"
    assert captured.pid == result.pid, "narrowing must preserve the child PID"
    assert captured.stdout == result.stdout, "narrowing must preserve captured stdout"
    assert captured.stderr == result.stderr, "narrowing must preserve captured stderr"
    assert captured.started_at == result.started_at, (
        "narrowing must preserve start time"
    )
    assert captured.duration == result.duration, "narrowing must preserve duration"
    assert captured.max_rss_bytes == result.max_rss_bytes, (
        "narrowing must preserve peak memory measurement"
    )
    assert captured.user_cpu_seconds == result.user_cpu_seconds, (
        "narrowing must preserve user CPU measurement"
    )
    assert captured.system_cpu_seconds == result.system_cpu_seconds, (
        "narrowing must preserve system CPU measurement"
    )
    assert captured.resolved_path == result.resolved_path, (
        "narrowing must preserve the resolved executable path"
    )
    assert captured.relay_fallbacks == (fallback,), (
        "narrowing must preserve relay fallback diagnostics"
    )
    assert captured.require_captured() is captured, (
        "narrowing an already captured result must preserve its identity"
    )


@pytest.mark.parametrize(
    ("stdout", "stderr", "missing"),
    [
        (None, "err", "stdout"),
        ("out", None, "stderr"),
        (None, None, "stdout and stderr"),
    ],
)
def test_command_result_require_captured_reports_missing_streams(
    stdout: str | None,
    stderr: str | None,
    missing: str,
) -> None:
    """Narrowing without both streams gives a useful capture diagnostic."""
    result = c.CommandResult(c.ECHO, (), 0, 1, stdout, stderr)

    with pytest.raises(ValueError, match=missing) as exc_info:
        result.require_captured()

    assert "RunOutputOptions(capture=True)" in str(exc_info.value)


def test_command_result_require_captured_rejects_non_text_streams() -> None:
    """Narrowing rejects runtime values that are not decoded text."""
    result = c.CommandResult(
        c.ECHO,
        (),
        0,
        1,
        typ.cast("str | None", b"out"),
        "err",
    )

    with pytest.raises(ValueError, match="stdout") as exc_info:
        result.require_captured()

    assert "not captured as text" in str(exc_info.value)
