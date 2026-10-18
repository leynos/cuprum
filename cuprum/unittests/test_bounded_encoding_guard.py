r"""Rejection of bounded text captures whose codec the trim cannot repair.

``BoundedCapture`` keeps a byte tail, and a cut can land inside a code point.
The trim at finalization knows how to repair that for UTF-8, for the
fixed-width UTF-16/UTF-32 codecs, and for single-byte encodings, where a byte
is its own boundary. It knows nothing about the multi-byte East Asian
families, whose trail bytes are indistinguishable from lead bytes once the
head is gone: a Shift-JIS slice ``b"\\x81a"`` cut after ``b"\\x81"`` would
decode the orphaned ``b"a"`` on its own and put a character on the result the
child never wrote.

These cases pin the classifier that decides which encodings are admissible,
and every pre-spawn entry point that applies it. The seams matter as much as
the classifier: a rule only one entry point consults is a rule the others
silently flout, so the refusal is asserted through ``run``, ``run_sync``,
``lines``, and a pipeline. ``run`` and ``run_sync`` reach the guard through
``_prepare_owned_command``; ``lines`` builds its execution at the call site;
the pipeline resolves its own configurations.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import (
    BoundedCapture,
    CapturePolicy,
    ExecutionContext,
    FileBackedCapture,
    FullCapture,
    RunOutputOptions,
)
from cuprum._bounded_encoding import (
    _is_trim_safe,
    _reject_unsupported_bounded_encoding,
)
from tests.helpers.catalogue import build_python_catalogue_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd

# Encodings a truncated tail can be repaired under, or that need no repair:
# the self-describing and fixed-width UTF codecs, the mark-selected family,
# the two registry names whose order a mark decides, and a spread of
# single-byte codecs across several alphabets.
_TRIM_SAFE = [
    pytest.param("utf-8", id="utf-8"),
    pytest.param("utf-8-sig", id="utf-8-sig"),
    pytest.param("utf-16", id="utf-16"),
    pytest.param("utf-16-le", id="utf-16-le"),
    pytest.param("utf-16-be", id="utf-16-be"),
    pytest.param("utf-32", id="utf-32"),
    pytest.param("utf-32-le", id="utf-32-le"),
    pytest.param("utf-32-be", id="utf-32-be"),
    pytest.param("ascii", id="ascii"),
    pytest.param("latin-1", id="latin-1"),
    pytest.param("cp1252", id="cp1252"),
    pytest.param("iso-8859-15", id="iso-8859-15"),
    pytest.param("koi8-r", id="koi8-r"),
    pytest.param("mac-roman", id="mac-roman"),
    pytest.param("cp437", id="cp437"),
    pytest.param("tis-620", id="tis-620"),
    # A single-byte codec whose bytes are not ASCII-compatible: admitting it
    # here is what keeps the rule about *width* rather than about ASCII.
    pytest.param("cp037", id="cp037"),
]

# Encodings whose trail bytes cannot be told from a lead byte once the head is
# gone. The escape families are here for a second reason: decoding every lone
# byte succeeds for them, so only the pending-byte probe reveals them.
_TRIM_UNSAFE = [
    pytest.param("shift_jis", id="shift_jis"),
    pytest.param("cp932", id="cp932"),
    pytest.param("euc_jp", id="euc_jp"),
    pytest.param("euc_kr", id="euc_kr"),
    pytest.param("gbk", id="gbk"),
    pytest.param("gb2312", id="gb2312"),
    pytest.param("gb18030", id="gb18030"),
    pytest.param("big5", id="big5"),
    pytest.param("cp949", id="cp949"),
    pytest.param("johab", id="johab"),
    pytest.param("hz", id="hz"),
    pytest.param("utf-7", id="utf-7"),
    pytest.param("iso2022_jp", id="iso2022_jp"),
    pytest.param("iso2022_kr", id="iso2022_kr"),
]

# Not text encodings at all. They are refused here rather than at decode time
# because a bounded capture is the *reason* they would be reached.
_TRANSFORM_CODECS = [
    pytest.param("base64", id="base64"),
    pytest.param("hex", id="hex"),
    pytest.param("zlib", id="zlib"),
    pytest.param("rot-13", id="rot-13"),
]


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a builder for the current interpreter, under a catalogue."""
    return build_python_catalogue_env().builder


@pytest.mark.parametrize("encoding", _TRIM_SAFE)
def test_a_repairable_encoding_is_admitted(encoding: str) -> None:
    """Every encoding the trim handles, or that needs no repair, is accepted."""
    assert _is_trim_safe(encoding), (
        f"{encoding!r} has boundaries the trim can find, so a bounded capture "
        "must not refuse it"
    )


@pytest.mark.parametrize("encoding", _TRIM_UNSAFE)
def test_a_multibyte_encoding_the_trim_cannot_repair_is_refused(
    encoding: str,
) -> None:
    """A trail byte a bound can orphan is what the refusal exists for."""
    assert not _is_trim_safe(encoding), (
        f"{encoding!r} can leave a trail byte that decodes as a character the "
        "child never wrote, so a bounded capture must refuse it"
    )


@pytest.mark.parametrize("encoding", _TRANSFORM_CODECS)
def test_a_transform_codec_is_refused(encoding: str) -> None:
    """A codec that is not a text encoding cannot render a capture at all."""
    assert not _is_trim_safe(encoding), (
        f"{encoding!r} is not a text encoding, so a bounded text capture must "
        "not admit it"
    )


def test_an_unknown_encoding_keeps_its_lookup_error_at_decode_time() -> None:
    """A name the registry does not know is not turned into a new refusal.

    The ``LookupError`` has always come from the decode, and a caller reading
    it there learns the same thing about the same name. Refusing at spawn
    instead would move an existing error to a new place for a reason that has
    nothing to do with the bound.
    """
    assert _is_trim_safe("no-such-encoding"), (
        "an unknown encoding must pass the bounded check so its LookupError "
        "still arrives from the decode"
    )


def _guard_call(
    policy: CapturePolicy | None,
    *,
    encoding: str,
    capture_bytes: bool,
) -> cabc.Callable[[], None]:
    """Bind one guard invocation, so a case can assert on running it."""
    return lambda: _reject_unsupported_bounded_encoding(
        policy, encoding=encoding, capture_bytes=capture_bytes
    )


# The policy, the mode, and the encoding are three axes of one decision, and
# only their combination decides whether a refusal is correct. Each row below
# changes exactly one of them from the refusing case.
@pytest.mark.parametrize(
    ("policy", "capture_bytes", "encoding", "is_refused"),
    [
        pytest.param(
            BoundedCapture(max_bytes=8), False, "shift_jis", True, id="refused"
        ),
        pytest.param(BoundedCapture(max_bytes=8), False, "utf-8", False, id="admitted"),
        # Every non-bounded policy renders no truncated tail, so the encoding
        # is irrelevant to it.
        pytest.param(None, False, "shift_jis", False, id="uncaptured"),
        pytest.param(FullCapture(), False, "shift_jis", False, id="full"),
        pytest.param(FileBackedCapture(), False, "shift_jis", False, id="file-backed"),
        # Byte mode never trims, so the encoding names only how a drain
        # renders text it no longer produces.
        pytest.param(BoundedCapture(max_bytes=8), True, "shift_jis", False, id="bytes"),
    ],
)
def test_the_guard_reads_policy_and_mode_not_just_encoding(
    policy: CapturePolicy | None,
    *,
    capture_bytes: bool,
    encoding: str,
    is_refused: bool,
) -> None:
    """A refusal keys on a bounded *text* capture, never on the encoding alone."""
    call = _guard_call(policy, encoding=encoding, capture_bytes=capture_bytes)

    if is_refused:
        with pytest.raises(ValueError, match="BoundedCapture requires an encoding"):
            call()
    else:
        call()


def test_a_refusal_names_the_policy_alternatives_and_the_encoding() -> None:
    """The message must say what to change, not merely that something is wrong."""
    with pytest.raises(ValueError, match="BoundedCapture requires an encoding") as info:
        _reject_unsupported_bounded_encoding(
            BoundedCapture(max_bytes=8), encoding="shift_jis", capture_bytes=False
        )

    message = str(info.value)
    assert "'shift_jis'" in message, (
        f"the refusal must quote the encoding; got {message!r}"
    )
    for alternative in ("FullCapture", "FileBackedCapture", "bytes"):
        assert alternative in message, (
            f"the refusal must name {alternative} as an escape; got {message!r}"
        )


def test_a_single_command_refuses_bounded_shift_jis_before_spawning(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """``run_sync`` reaches the guard through its own preparation seam."""
    command = python_builder("-c", "print('hi')")

    with pytest.raises(ValueError, match="BoundedCapture requires an encoding"):
        command.run_sync(
            output=RunOutputOptions(capture_stdout=BoundedCapture(max_bytes=4)),
            context=ExecutionContext(encoding="shift_jis"),
        )


def test_the_refusal_covers_the_stderr_policy_too(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The two streams are checked independently, so stderr is not missed."""
    command = python_builder("-c", "print('hi')")

    with pytest.raises(ValueError, match="BoundedCapture requires an encoding"):
        command.run_sync(
            output=RunOutputOptions(capture_stderr=BoundedCapture(max_bytes=4)),
            context=ExecutionContext(encoding="shift_jis"),
        )


def test_the_shorthand_cannot_smuggle_a_bounded_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """``capture=True`` resolves to ``FullCapture``, which needs no repair."""
    command = python_builder("-c", "import sys; sys.stdout.write('hi')")
    result = command.run_sync(
        output=RunOutputOptions(capture=True),
        context=ExecutionContext(encoding="shift_jis"),
    )

    assert result.stdout == "hi", (
        "a full capture of a Shift-JIS stream is not a truncated tail, so it "
        f"must run; got {result.stdout!r}"
    )


def test_a_bounded_byte_capture_admits_any_encoding(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Byte mode keeps the child's bytes, so the codec never sees a cut."""
    command = python_builder("-c", "import sys; sys.stdout.buffer.write(b'hi')")
    result = command.run_bytes_sync(
        output=RunOutputOptions(capture_stdout=BoundedCapture(max_bytes=4)),
        context=ExecutionContext(encoding="shift_jis"),
    )

    assert result.stdout == b"hi", (
        "a byte-exact bounded capture has no code-point edge to repair, so it "
        f"must admit Shift-JIS; got {result.stdout!r}"
    )


def test_an_async_run_refuses_bounded_shift_jis_before_spawning(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The awaited entry point refuses through the same preparation seam.

    ``run_sync`` is a wrapper around ``run``, so this case reaches the guard
    from the coroutine itself rather than from its synchronous twin: the
    refusal must arrive before the first await either way.
    """
    command = python_builder("-c", "print('hi')")

    async def _run() -> None:
        await command.run(
            output=RunOutputOptions(capture_stdout=BoundedCapture(max_bytes=4)),
            context=ExecutionContext(encoding="shift_jis"),
        )

    with pytest.raises(ValueError, match="BoundedCapture requires an encoding"):
        asyncio.run(_run())


def test_line_iteration_refuses_bounded_shift_jis_before_spawning(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """``lines`` builds its execution directly, without ``run``'s preparation.

    The line path assembles the subprocess execution at the call site rather
    than through the owned-command helper ``run`` uses, so it is the second
    seam that has to consult the guard. The refusal is raised while the stream
    is being built, before the caller ever iterates it.
    """
    command = python_builder("-c", "print('hi')")

    with pytest.raises(ValueError, match="BoundedCapture requires an encoding"):
        command.lines(
            output=RunOutputOptions(capture_stdout=BoundedCapture(max_bytes=4)),
            context=ExecutionContext(encoding="shift_jis"),
        )


def test_a_pipeline_refuses_bounded_shift_jis_before_spawning(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The pipeline seam is separate from the command seam and must agree."""
    first, second = (
        python_builder("-c", "print('a')"),
        python_builder("-c", "print('b')"),
    )

    with pytest.raises(ValueError, match="BoundedCapture requires an encoding"):
        (first | second).run_sync(
            output=RunOutputOptions(capture_stdout=BoundedCapture(max_bytes=4)),
            context=ExecutionContext(encoding="shift_jis"),
        )
