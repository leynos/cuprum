"""Unit tests for the byte-exact ``run_bytes()`` command path.

Covers the explicit binary-output mode beside the text-mode behaviour in
``test_safe_cmd_output.py``: both entry points, disabled capture, the
presentation sink's binary buffer, and the option combinations bytes mode
refuses before it spawns.

The child writes a payload no decoder can round-trip — every byte value, then
an invalid UTF-8 sequence and a NUL — so a run that quietly decoded would fail
rather than pass by luck.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import io
import typing as typ

import pytest

from cuprum import sh
from cuprum.context import ScopeConfig
from cuprum.echo_events import EchoErrorCategory, EchoStream
from cuprum.sh import (
    BytesCommandResult,
    CommandResult,
    ExecutionContext,
    RunOutputOptions,
    StdinInput,
    scoped,
)
from tests.helpers.catalogue import (
    python_builder as build_python_builder,
    python_catalogue,
)
from tests.helpers.execution import _RunKwargs

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineEvent
    from cuprum.program import Program
    from cuprum.sh import SafeCmd

# Every byte value, then a lone continuation byte, a byte no UTF-8 sequence
# starts with, a second lone continuation byte, and a NUL. A decoding run
# replaces the four invalid ones and keeps the rest, so any difference is the
# mode leaking.
_FAILING_PAYLOAD = bytes(range(256)) + b"\xff\x00\xfe\x80"
_WRITE_BOTH_STREAMS = (
    "import sys;"
    f"sys.stdout.buffer.write({_FAILING_PAYLOAD!r});"
    f"sys.stderr.buffer.write({_FAILING_PAYLOAD!r})"
)
_ECHO_STDIN = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


type BytesExecuteFn = cabc.Callable[[SafeCmd, _RunKwargs], BytesCommandResult]


@dc.dataclass(frozen=True, slots=True)
class ObserveScope:
    """An allowlist paired with a command it admits."""

    allowlist: frozenset[Program]
    cmd: SafeCmd


def _run_bytes_async(cmd: SafeCmd, kwargs: _RunKwargs) -> BytesCommandResult:
    """Execute a command through the asynchronous byte-exact entry point."""
    return asyncio.run(cmd.run_bytes(**kwargs))


def _run_bytes_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> BytesCommandResult:
    """Execute a command through the synchronous byte-exact entry point."""
    return cmd.run_bytes_sync(**kwargs)


@pytest.fixture(
    params=[_run_bytes_async, _run_bytes_sync],
    ids=["run_bytes()", "run_bytes_sync()"],
)
def byte_entry_point(request: pytest.FixtureRequest) -> BytesExecuteFn:
    """Provide each byte-exact entry point behind one callable shape."""
    return typ.cast("BytesExecuteFn", request.param)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


@pytest.fixture
def bytes_cmd(python_builder: cabc.Callable[..., SafeCmd]) -> SafeCmd:
    """Build the command whose two streams carry the binary payload."""
    return python_builder("-c", _WRITE_BOTH_STREAMS)


@pytest.fixture
def observe_scope() -> ObserveScope:
    """Provide an allowlist and a binary-payload command it admits.

    Both must come from one catalogue: the allowlist admits only the program
    that catalogue registered, so a command built from a second catalogue
    would be refused before the drain this test exercises.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    return ObserveScope(
        allowlist=catalogue.allowlist,
        cmd=python("-c", _WRITE_BOTH_STREAMS),
    )


def test_byte_exact_capture_round_trips_both_streams(
    byte_entry_point: BytesExecuteFn,
    bytes_cmd: SafeCmd,
) -> None:
    """Both captured streams hold the child's bytes, decoded nowhere."""
    result = byte_entry_point(bytes_cmd, {})

    assert isinstance(result, BytesCommandResult), (
        f"run_bytes must report a BytesCommandResult, got {type(result).__name__}"
    )
    assert result.stdout == _FAILING_PAYLOAD, (
        f"stdout must survive byte-for-byte, got {result.stdout!r}"
    )
    assert result.stderr == _FAILING_PAYLOAD, (
        f"stderr must survive byte-for-byte, got {result.stderr!r}"
    )
    assert result.exit_code == 0, "the child exits cleanly after writing"


def test_text_mode_is_unchanged_beside_the_byte_exact_entry_point(
    bytes_cmd: SafeCmd,
) -> None:
    """The ordinary entry point still decodes, and still returns its own class."""
    result = asyncio.run(bytes_cmd.run())

    assert type(result) is CommandResult, (
        f"run() must keep reporting a CommandResult, got {type(result).__name__}"
    )
    assert result.stdout == _FAILING_PAYLOAD.decode("utf-8", errors="replace"), (
        "run() must keep decoding with the configured error handler, got "
        f"{result.stdout!r}"
    )


def test_captured_stream_with_no_output_is_empty_bytes(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A captured stream the child left untouched reports ``b""``, not ``None``."""
    quiet = python_builder("-c", "pass")

    result = asyncio.run(quiet.run_bytes())

    assert result.stdout == b"", f"an empty capture is b'' , got {result.stdout!r}"
    assert result.stderr == b"", f"an empty capture is b'' , got {result.stderr!r}"


def test_disabled_capture_reports_none_for_both_streams(bytes_cmd: SafeCmd) -> None:
    """``capture=False`` yields ``None`` in bytes mode exactly as in text mode."""
    result = _run_bytes_async(bytes_cmd, {"output": RunOutputOptions(capture=False)})

    assert result.stdout is None, (
        f"a disabled capture must report None, got {result.stdout!r}"
    )
    assert result.stderr is None, (
        f"a disabled capture must report None, got {result.stderr!r}"
    )


def test_echo_presents_bytes_through_the_sink_binary_buffer(bytes_cmd: SafeCmd) -> None:
    """Echoed bytes reach a sink's binary buffer without a decoding detour."""
    sink = _BinaryRecordingSink()

    result = _run_bytes_async(
        bytes_cmd,
        {
            "output": RunOutputOptions(echo=True),
            "context": ExecutionContext(
                stdout_sink=typ.cast("typ.IO[str]", sink),
                stderr_sink=typ.cast("typ.IO[str]", sink),
            ),
        },
    )

    assert result.stdout == _FAILING_PAYLOAD, "capture must be unaffected by echo"
    presented = sink.buffer.getvalue()
    assert _FAILING_PAYLOAD in presented, (
        f"the echoed payload must reach the sink's buffer intact, got {presented!r}"
    )


def test_capture_survives_a_text_only_sink_encode_failure(bytes_cmd: SafeCmd) -> None:
    """A sink that cannot render the bytes still leaves the capture intact."""
    sink = _AsciiOnlySink()

    result = _run_bytes_async(
        bytes_cmd,
        {
            "output": RunOutputOptions(echo=True),
            "context": ExecutionContext(
                stdout_sink=typ.cast("typ.IO[str]", sink),
                stderr_sink=typ.cast("typ.IO[str]", sink),
            ),
        },
    )

    assert sink.attempted, "the sink must have been offered the payload"
    assert result.stdout == _FAILING_PAYLOAD, (
        "capture is taken from the child's stream, not the sink, so a render "
        f"failure cannot truncate it; got {result.stdout!r}"
    )
    assert result.stderr == _FAILING_PAYLOAD, (
        f"the untouched stream's capture must be complete too, got {result.stderr!r}"
    )
    disabled = {fallback.stream for fallback in result.relay_fallbacks}
    assert disabled == {EchoStream.STDOUT, EchoStream.STDERR}, (
        "each stream whose sink rejected the payload must be reported as "
        f"disabled, got {result.relay_fallbacks!r}"
    )
    assert {fallback.error_category for fallback in result.relay_fallbacks} == {
        EchoErrorCategory.UNICODE_ENCODE
    }, (
        "the reported category must name the encoding failure, got "
        f"{result.relay_fallbacks!r}"
    )


def test_line_observation_is_refused_before_the_child_spawns(
    bytes_cmd: SafeCmd,
) -> None:
    """``on_line`` cannot be honoured in bytes mode, and says so up front."""
    observed: list[LineEvent] = []

    def observe(event: LineEvent) -> None:
        """Record a line event, which must never arrive."""
        observed.append(event)

    with pytest.raises(ValueError, match="on_line"):
        _run_bytes_async(bytes_cmd, {"output": RunOutputOptions(on_line=observe)})

    assert not observed, "the rejection must happen before any line is observed"


def test_line_observation_is_refused_before_the_sync_entry_point_runs(
    bytes_cmd: SafeCmd,
) -> None:
    """The synchronous entry point validates before it starts its own loop."""

    def observe(_event: LineEvent) -> None:
        """Accept a line event no byte-exact run may deliver."""

    with pytest.raises(ValueError, match="on_line"):
        _run_bytes_sync(bytes_cmd, {"output": RunOutputOptions(on_line=observe)})


def test_observe_hooks_do_not_break_byte_exact_capture(
    observe_scope: ObserveScope,
) -> None:
    """A registered observe hook must not cost bytes mode its exactness.

    The observe machinery supplies an internal line sink the caller never
    asked for, so a run that merely *has* a hook registered must still return
    the child's bytes. Both channels must hold at once: the capture stays
    byte-exact, and the observer receives the decoded line.
    """
    observed: list[str] = []

    def hook(event: LineEvent) -> None:
        """Record the decoded line the run publishes."""
        line = getattr(event, "line", None)
        if line is not None:
            observed.append(line)

    with scoped(ScopeConfig(allowlist=observe_scope.allowlist)), sh.observe(hook):
        result = _run_bytes_sync(observe_scope.cmd, {})

    assert result.stdout == _FAILING_PAYLOAD, (
        "an observe hook must not decode the captured payload, got "
        f"{result.stdout!r}"
    )
    assert result.stderr == _FAILING_PAYLOAD, (
        f"stderr must stay byte-exact beside a hook, got {result.stderr!r}"
    )
    assert observed, "the observe hook must still receive decoded lines"


def test_bytes_entry_point_accepts_a_text_stdin_input(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Text stdin and byte-exact capture compose without a mode conversion."""
    echo = python_builder("-c", _ECHO_STDIN)

    result = asyncio.run(echo.run_bytes(stdin=StdinInput(text="payload")))

    assert result.stdout == b"payload", (
        "text stdin must reach the child encoded, while the reply stays bytes; "
        f"got {result.stdout!r}"
    )


def test_bytes_entry_point_round_trips_binary_stdin(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Binary stdin reaches the child unchanged and returns the same way."""
    echo = python_builder("-c", _ECHO_STDIN)

    result = asyncio.run(echo.run_bytes(stdin=StdinInput(data=_FAILING_PAYLOAD)))

    assert result.stdout == _FAILING_PAYLOAD, (
        f"binary stdin must round-trip unchanged, got {result.stdout!r}"
    )


class _BinaryRecordingSink:
    """A presentation sink whose buffer collects the echoed bytes verbatim."""

    def __init__(self) -> None:
        """Start with an empty byte buffer and no text writes."""
        self.buffer = io.BytesIO()
        self.writes: list[str] = []

    def write(self, payload: str) -> int:
        """Record a text write, which the binary fast path must never take."""
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


class _AsciiOnlySink:
    """A sink that refuses every byte the ASCII codec cannot render."""

    def __init__(self) -> None:
        """Record whether the sink was offered anything at all."""
        self.attempted = False

    def write(self, payload: str) -> int:
        """Reject payloads the ASCII codec cannot represent."""
        self.attempted = True
        payload.encode("ascii")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""
