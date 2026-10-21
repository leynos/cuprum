"""Which result class a settled single-command text run reports.

``CapturedResult`` promises both streams are decoded text, so the runtime class
is a statement about the payloads a run produced, not about the configuration
that produced them. Two configurations break the equivalence: a run that
captures one stream alone leaves the other ``None``, and a
:class:`~cuprum.capture_policy.FileBackedCapture` that spilled reports its
stream as ``None`` because the text is in the file the record names.

Each case asserts the class *and* the payloads it was chosen for. Asserting the
class alone would pass for a run narrowed into a class whose ``stderr`` is
``None`` every time the type were loosened; asserting the payloads alone would
pass for a run that reported the right text under the wrong guarantee. The
default case is the companion that keeps the narrowing from being given up
altogether: a run that captures both streams in full must still narrow.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import sh
from cuprum.capture_policy import BoundedCapture, FileBackedCapture
from cuprum.sh import CapturedResult, CommandResult, RunOutputOptions
from tests.helpers.catalogue import build_python_catalogue_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum.sh import SafeCmd

_STDOUT_TEXT = "stdout-text"
_STDERR_TEXT = "stderr-text"
_WRITE_BOTH = (
    f"import sys;sys.stdout.write({_STDOUT_TEXT!r});sys.stderr.write({_STDERR_TEXT!r})"
)
# Comfortably over the spill threshold, so every spilled case really crosses it.
_SPILL_PAYLOAD = "z" * 512
_SPILL_BOTH = (
    f"import sys;sys.stdout.write({_SPILL_PAYLOAD!r});"
    f"sys.stderr.write({_SPILL_PAYLOAD!r})"
)


def _builder() -> cabc.Callable[..., SafeCmd]:
    """Return a builder for the interpreter this suite runs under."""
    return build_python_catalogue_env().builder


def test_a_fully_captured_run_narrows_and_carries_its_records() -> None:
    """Both streams in full is the shape ``CapturedResult`` was introduced for.

    The records travel with the narrowed class as well, so a caller that
    switches on ``captures`` does not have to know which class it received.
    """
    result = _builder()("-c", _WRITE_BOTH).run_sync(output=RunOutputOptions())

    assert type(result) is CapturedResult, (
        f"a fully captured text run must narrow; got {type(result).__name__}"
    )
    assert (result.stdout, result.stderr) == (_STDOUT_TEXT, _STDERR_TEXT), (
        "the narrowed streams must be the text the child wrote; got "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert [record.stream for record in result.captures] == ["stdout", "stderr"], (
        "the narrowed result must carry both capture records; got "
        f"{[record.stream for record in result.captures]!r}"
    )


def test_a_capture_of_one_stream_alone_does_not_narrow() -> None:
    """The uncaptured stream is ``None``, which the narrowed class cannot report.

    Narrowing here would have to claim the run captured a stream it did not,
    and ``require_captured()`` would then return a result whose ``stderr`` is
    ``None`` despite being typed ``str``.
    """
    options = RunOutputOptions(capture_stdout=True, capture_stderr=False)

    result = _builder()("-c", _WRITE_BOTH).run_sync(output=options)

    assert type(result) is CommandResult, (
        f"a single-stream capture must not narrow; got {type(result).__name__}"
    )
    assert result.stdout == _STDOUT_TEXT, (
        f"the captured stream must keep its text; got {result.stdout!r}"
    )
    assert result.stderr is None, (
        f"the uncaptured stream must stay None; got {result.stderr!r}"
    )
    assert [record.stream for record in result.captures] == ["stdout"], (
        "the result must record exactly the stream it captured; got "
        f"{[record.stream for record in result.captures]!r}"
    )
    with pytest.raises(ValueError, match="stderr"):
        result.require_captured()


def test_a_text_spill_reports_a_plain_result_naming_the_file(
    tmp_path: pathlib.Path,
) -> None:
    """The spilled stream's text is at the record's path, not in the payload.

    The class choice and the record are two halves of one fact: a
    ``CapturedResult`` here would have to put an empty string where the child
    wrote a whole stream, and the record is what sends the caller to the file.
    """
    options = RunOutputOptions(
        capture_stdout=FileBackedCapture(
            spill_threshold_bytes=8,
            directory=tmp_path,
            keep_file=True,
        ),
        capture_stderr=False,
    )

    result = _builder()(
        "-c", f"import sys;sys.stdout.write({_SPILL_PAYLOAD!r})"
    ).run_sync(output=options)

    assert type(result) is CommandResult, (
        f"a spilled text run must not narrow; got {type(result).__name__}"
    )
    assert result.stdout is None, (
        f"a spilled stream must report no text; got {result.stdout!r}"
    )
    record = result.captures[0]
    assert record.storage == "file", (
        f"the record must say the stream moved to a file; got {record.storage!r}"
    )
    assert record.path is not None, "a spilled capture must name its file"
    assert record.path.read_text() == _SPILL_PAYLOAD, (
        "the file the record names must hold the child's whole stream; got "
        f"{record.path.read_text()!r}"
    )


def test_a_bounded_stream_still_narrows_when_both_streams_are_text() -> None:
    """A bound shortens a stream without making it absent, so the class holds.

    A bounded capture retains a tail the caller can read, which is exactly what
    ``CapturedResult`` promises; the record beside it says the tail is short.
    """
    options = RunOutputOptions(
        capture_stdout=BoundedCapture(max_bytes=4),
        capture_stderr=True,
    )

    result = _builder()("-c", _WRITE_BOTH).run_sync(output=options)

    assert type(result) is CapturedResult, (
        f"a bounded run with both streams as text must narrow; got "
        f"{type(result).__name__}"
    )
    assert result.stdout == _STDOUT_TEXT[-4:], (
        f"the narrowed stream must be the retained tail; got {result.stdout!r}"
    )
    assert result.captures[0].dropped_bytes == len(_STDOUT_TEXT) - 4, (
        "the record must report the bytes the bound discarded; got "
        f"{result.captures[0].dropped_bytes}"
    )


def test_a_spilled_byte_run_reports_its_records_without_narrowing() -> None:
    """Byte mode has its own class, and it carries the same records.

    The record is the only way a byte run can report a spill: the payload of a
    byte-exact file-backed capture is read back from the file, so a caller
    comparing payloads alone cannot tell the two storages apart.
    """
    options = RunOutputOptions(
        capture_stdout=FileBackedCapture(
            spill_threshold_bytes=8,
            directory=None,
            keep_file=False,
        ),
        capture_stderr=False,
    )

    result = _builder()(
        "-c", f"import sys;sys.stdout.write({_SPILL_PAYLOAD!r})"
    ).run_bytes_sync(output=options)

    assert isinstance(result, sh.BytesCommandResult), (
        f"a byte run must report a BytesCommandResult; got {type(result).__name__}"
    )
    assert result.stdout == _SPILL_PAYLOAD.encode(), (
        f"byte mode must return the stream's own bytes; got {result.stdout!r}"
    )
    assert result.captures[0].storage == "file", (
        "the record must report where the bytes were kept; got "
        f"{result.captures[0].storage!r}"
    )
