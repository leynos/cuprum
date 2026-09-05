"""Unit tests for SafeCmd output handling.

Covers capture and echo behaviour on ``run()``/``run_sync()``: the
backward-compatible ``IOOptions`` alias, per-stream echo control, injected
echo sinks, decoding with the configured encoding, and echo-sink
encode-failure containment.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum import ECHO, sh
from cuprum.sh import CommandResult, ExecutionContext, IOOptions, RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.execution import ExecuteFn, _RunKwargs, assert_capture_disabled

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parameterized execution strategies for run() and run_sync().

    Returns
    -------
    tuple[str, ExecuteFn]
        The strategy label and its execution callable.
    """
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


def test_io_options_warns_when_constructed() -> None:
    """The backward-compatible IOOptions alias warns on construction."""
    with pytest.warns(
        DeprecationWarning,
        match=r"IOOptions is deprecated; use RunOutputOptions instead",
    ):
        options = IOOptions(capture=False, echo=True)

    assert options.capture is False
    assert options.echo is True


def test_io_options_resolves_per_stream_echo() -> None:
    """IOOptions(echo=True) resolves both streams despite the warning path."""
    with pytest.warns(DeprecationWarning, match="IOOptions is deprecated"):
        options = IOOptions(echo=True)

    assert options.resolved_echo == (True, True), (
        "the deprecated alias must resolve the inherited per-stream fields, "
        f"got {options.resolved_echo!r}"
    )


def test_captures_stderr_only(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() capture stderr independently."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        'import sys; print("err", file=sys.stderr)',
    )

    result = execute(command, {})

    assert result.exit_code == 0
    assert result.ok is True
    assert result.stdout == ""
    assert result.stderr is not None
    assert result.stderr.strip() == "err"


def test_captures_and_echoes_stderr(
    python_builder: cabc.Callable[..., SafeCmd],
    capsys: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() echo stderr and capture it separately."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        'import sys; print("err", file=sys.stderr)',
    )

    result = execute(command, {"output": RunOutputOptions(echo=True)})

    captured = capsys.readouterr()

    assert result.exit_code == 0
    assert result.ok is True
    assert result.stdout == ""
    assert result.stderr is not None
    assert result.stderr.strip() == "err"
    assert captured.out == ""
    assert captured.err.strip() == "err"


def test_echoes_when_requested(
    capfd: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() echo output to stdout while still capturing it."""
    _, execute = execution_strategy
    command = sh.make(ECHO)("hello runtime")

    result = execute(command, {"output": RunOutputOptions(echo=True)})

    captured = capfd.readouterr()
    assert result.stdout is not None
    assert "hello runtime" in captured.out
    assert result.stdout.strip() == "hello runtime"


@pytest.mark.usefixtures("execution_strategy")
def test_captures_stdout_silently(
    python_builder: cabc.Callable[..., SafeCmd],
    capsys: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Captured-but-not-echoed stdout writes nothing and is fully returned.

    This is the lading `cargo metadata` probe case: the document must stay out
    of the CI log (parent stdout) while remaining complete on the result.
    """
    _, execute = execution_strategy
    command = python_builder("-c", 'import sys; sys.stdout.write("doc")')

    result = execute(
        command,
        {"output": RunOutputOptions(capture=True, echo_stdout=False)},
    )

    captured = capsys.readouterr()

    assert captured.out == "", (
        "a non-echoed stream must not write to the parent's stdout"
    )
    assert result.stdout == "doc", (
        "a non-echoed stream must still be captured in full on the result"
    )


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_per_stream_echo_is_independent(
    python_builder: cabc.Callable[..., SafeCmd],
    capsys: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
    *,
    stream: str,
) -> None:
    """Echoing one stream leaves the other stream out of the parent process.

    Parameters
    ----------
    stream : str
        The stream selected for echo; the other stream must stay silent.
    """
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        'import sys; print("out"); print("err", file=sys.stderr)',
    )
    echo_stdout = stream == "stdout"

    result = execute(
        command,
        {
            "output": RunOutputOptions(
                capture=True,
                echo_stdout=echo_stdout,
                echo_stderr=not echo_stdout,
            ),
        },
    )
    captured = capsys.readouterr()

    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert captured.out.strip() == ("out" if echo_stdout else ""), (
        f"stdout echo must follow echo_stdout={echo_stdout}"
    )
    assert captured.err.strip() == ("" if echo_stdout else "err"), (
        f"stderr echo must follow echo_stderr={not echo_stdout}"
    )


def test_stderr_echo_is_unaffected_by_stdout_setting(
    python_builder: cabc.Callable[..., SafeCmd],
    capsys: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Muting stdout echo leaves stderr echo working, and vice versa."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        'import sys; print("out"); print("err", file=sys.stderr)',
    )

    result = execute(
        command,
        {
            "output": RunOutputOptions(
                capture=True,
                echo_stdout=False,
                echo_stderr=True,
            ),
            "context": ExecutionContext(
                stdout_sink=io.StringIO(),
                stderr_sink=io.StringIO(),
            ),
        },
    )
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == "", "injected sinks must keep both streams off capsys"
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_allows_disabling_capture(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() execute without retaining output when disabled."""
    _, execute = execution_strategy
    command = sh.make(ECHO)("uncaptured output")

    result = execute(command, {"output": RunOutputOptions(capture=False)})

    assert_capture_disabled(result)


def test_echoes_to_custom_sinks(
    python_builder: cabc.Callable[..., SafeCmd],
    capsys: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() direct echo output to injected sinks."""
    _, execute = execution_strategy
    stdout_sink = io.StringIO()
    stderr_sink = io.StringIO()
    command = python_builder(
        "-c",
        'import sys; print("out"); print("err", file=sys.stderr)',
    )

    result = execute(
        command,
        {
            "output": RunOutputOptions(echo=True),
            "context": ExecutionContext(
                stdout_sink=stdout_sink,
                stderr_sink=stderr_sink,
            ),
        },
    )
    captured = capsys.readouterr()

    assert result.stdout is not None
    assert result.stderr is not None
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert stdout_sink.getvalue().strip() == "out"
    assert stderr_sink.getvalue().strip() == "err"
    assert captured.out == ""
    assert captured.err == ""


def test_decodes_with_configured_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() use configured encoding/errors for decoding."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        ("import sys; sys.stdout.buffer.write(bytes([0x96])); sys.stdout.flush()"),
    )

    result = execute(
        command,
        {
            "context": ExecutionContext(
                encoding="cp1252",
                errors="strict",
            ),
        },
    )

    assert result.exit_code == 0
    assert result.ok is True
    assert result.stdout == "\u2013"
    assert result.stderr == ""


class _Cp1252TextOnlySink:
    """Text-only sink modelling a parent stream too narrow for the output."""

    def __init__(self) -> None:
        """Record write attempts."""
        self.writes: list[str] = []

    def write(self, payload: str) -> int:
        """Reject payloads the CP1252 codec cannot represent."""
        self.writes.append(payload)
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


class _Cp1252BufferedSink:
    """Text sink exposing a binary ``buffer`` for the echo fast path."""

    def __init__(self) -> None:
        """Pair a narrow text wrapper with its own binary buffer."""
        self.buffer = io.BytesIO()
        self.writes: list[str] = []

    def write(self, payload: str) -> int:
        """Reject payloads the CP1252 codec cannot represent."""
        self.writes.append(payload)
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


_UNICODE_PAYLOAD = "print('Cargo metadata: ś ń')"


def test_capture_survives_text_sink_encode_failure(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """A UnicodeEncodeError from a text sink must not abort the run."""
    _, execute = execution_strategy
    sink = _Cp1252TextOnlySink()
    command = python_builder("-c", _UNICODE_PAYLOAD)

    result = execute(
        command,
        {
            "output": RunOutputOptions(capture=True, echo=True),
            "context": ExecutionContext(
                stdout_sink=typ.cast("typ.IO[str]", sink),
            ),
        },
    )

    assert result.ok is True
    assert result.stdout == "Cargo metadata: ś ń\n"
    assert sink.writes == ["Cargo metadata: ś ń\n"], (
        "the rejected chunk must be the only attempted write for "
        f"writes={sink.writes!r}"
    )


def test_buffered_sink_receives_exact_bytes_despite_narrow_text_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """A sink with a binary ``buffer`` keeps receiving original bytes."""
    _, execute = execution_strategy
    sink = _Cp1252BufferedSink()
    command = python_builder("-c", _UNICODE_PAYLOAD)

    result = execute(
        command,
        {
            "output": RunOutputOptions(capture=True, echo=True),
            "context": ExecutionContext(
                stdout_sink=typ.cast("typ.IO[str]", sink),
            ),
        },
    )

    assert result.ok is True
    assert result.stdout == "Cargo metadata: ś ń\n"
    assert sink.buffer.getvalue() == "Cargo metadata: ś ń\n".encode(), (
        "buffered sinks must receive original bytes without transliteration for "
        f"received={sink.buffer.getvalue()!r}"
    )
    assert sink.writes == [], (
        "text write must not be used when a binary buffer exists for "
        f"writes={sink.writes!r}"
    )


def test_stdout_and_stderr_disable_echo_independently(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """A failing stream stops echoing while the other stream keeps echoing."""
    _, execute = execution_strategy
    stdout_sink = _Cp1252TextOnlySink()
    stderr_sink = _Cp1252TextOnlySink()
    command = python_builder(
        "-c",
        (
            "import sys; print('Cargo metadata: ś ń'); "
            "print('plain stderr text', file=sys.stderr)"
        ),
    )

    result = execute(
        command,
        {
            "output": RunOutputOptions(capture=True, echo=True),
            "context": ExecutionContext(
                stdout_sink=typ.cast("typ.IO[str]", stdout_sink),
                stderr_sink=typ.cast("typ.IO[str]", stderr_sink),
            ),
        },
    )

    assert result.ok is True
    assert result.stdout == "Cargo metadata: ś ń\n"
    assert result.stderr == "plain stderr text\n"
    assert "".join(stderr_sink.writes) == "plain stderr text\n", (
        "the plain-encoding stream must keep echoing for "
        f"stderr writes={stderr_sink.writes!r}"
    )
    assert "ś" in stdout_sink.writes[-1], (
        "the final stdout attempt must be the chunk carrying the "
        f"unencodable character for stdout writes={stdout_sink.writes!r}"
    )
    preceding = "".join(stdout_sink.writes[:-1])
    assert preceding.encode().decode("cp1252") in {"", "Cargo metadata: "}, (
        "preceding encodable stdout chunks must still have echoed for "
        f"stdout writes={stdout_sink.writes!r}, preceding={preceding!r}"
    )
