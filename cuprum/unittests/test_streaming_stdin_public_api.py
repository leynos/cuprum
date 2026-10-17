"""Public-surface tests for the streaming stdin and redirection vocabulary.

Split from ``test_public_api.py`` when this branch pushed that module past the
400-line ceiling. These tests are a subject of their own rather than a tail of
the export checks: each one pins a *contract* the new surface exposes — which
names are exported, what the widened ``stdin=`` parameter accepts, and which
option combinations are refused where.

The split follows the seam the tests themselves draw. Everything here concerns
``StdinStream``, ``StdinSource``, and ``StdioTarget``; nothing in the sibling
module reaches for them, so a change to this feature fails here rather than in
a module about capture, events, and catalogue exports.

Rejections are asserted with ``pytest.raises`` and the field name, because the
value of a rejection is as much *where* it happens as *that* it happens. The
``lines()`` case in particular cannot move to ``RunOutputOptions``: the same
options object is valid for ``run()``, so the requirement is only known at the
call that needs a pipe.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from pathlib import Path

import pytest

import cuprum as c

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def test_streaming_stdin_names_are_exported() -> None:
    """The streaming stdin and redirection vocabulary is public from both roots."""
    from cuprum import sh

    # Pinned by identity against the defining module so dropping a re-export, or
    # re-pointing one at a different definition, fails here rather than in a
    # caller's import.
    assert c.StdinStream is sh.StdinStream, (
        "StdinStream must be exported from cuprum.sh"
    )
    assert c.StdinSourceError is sh.StdinSourceError, (
        "StdinSourceError must be exported from cuprum.sh"
    )
    assert c.StdioTarget is sh.StdioTarget, (
        "StdioTarget must be exported from cuprum.sh"
    )
    assert c.StdinSource is sh.StdinSource, (
        "StdinSource must be exported from cuprum.sh"
    )
    for name in ("StdinStream", "StdinSourceError", "StdioTarget", "StdinSource"):
        assert name in c.__all__, f"{name} must appear in cuprum.__all__"
        assert name in sh.__all__, f"{name} must appear in cuprum.sh.__all__"


def test_stdin_parameter_accepts_both_source_forms() -> None:
    """``stdin=`` resolves to the widened union and takes either form.

    The annotation is checked through ``typing.get_type_hints`` because
    ``cuprum/sh/safe_cmd.py`` deliberately omits ``from __future__ import
    annotations``: public signatures are evaluated eagerly there, and this is
    the test that holds that contract for the widened parameter.
    """
    from cuprum import sh

    expected = sh.StdinSource | None
    for qualname in ("SafeCmd.run", "SafeCmd.run_sync", "SafeCmd.lines"):
        owner_name, method_name = qualname.split(".")
        method = getattr(getattr(sh, owner_name), method_name)
        hints = typ.get_type_hints(method)
        assert hints["stdin"] == expected, (
            f"{qualname} must accept StdinSource | None, got {hints['stdin']!r}"
        )
    # ``StdinSource`` is a PEP 695 alias, so the annotation holds the alias
    # itself rather than a flattened union. Check the alias's own value for the
    # union shape instead: ``typ.get_args`` on the annotation would only ever
    # show the alias as one member.
    assert sh.StdinSource.__value__ == sh.StdinInput | sh.StdinStream, (
        "StdinSource must be exactly the payload-or-stream union"
    )


def test_stdin_stream_wraps_an_async_iterable() -> None:
    """``StdinStream`` carries the producer unmodified and stays frozen."""

    async def producer() -> cabc.AsyncIterator[bytes]:
        """Yield one chunk, suspending first as a real producer would."""
        # The yield point is what makes the object an async *generator* rather
        # than a coroutine returning one, so the suspension is load-bearing.
        await asyncio.sleep(0)
        yield b""

    stream = c.StdinStream(producer())
    assert stream.chunks is not None, "the producer must be retained as given"
    with pytest.raises(dc.FrozenInstanceError):
        stream.chunks = producer()  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_stdin_source_error_is_a_cuprum_owned_exception() -> None:
    """``StdinSourceError`` is raised for producer and encoding failures."""
    assert issubclass(c.StdinSourceError, Exception), (
        "StdinSourceError must be an Exception subclass"
    )
    assert not issubclass(c.StdinSourceError, asyncio.CancelledError), (
        "cancellation must not be swallowed as a source failure"
    )


def test_stdio_target_variants_are_distinguishable() -> None:
    """Each ``StdioTarget`` variant carries the resource it names."""
    pipe = c.StdioTarget.pipe()
    inherited = c.StdioTarget.inherit()
    opened = c.StdioTarget.path(Path("out.log"))
    assert pipe != inherited != opened, "variants must not compare equal"
    assert c.StdioTarget.path(Path("out.log")) == opened, (
        "equal paths must compare equal so options stay value-like"
    )


def test_output_options_reject_a_target_for_a_captured_stream() -> None:
    """A redirected stream cannot also be captured or echoed.

    Capture and echo read from a parent-side pipe; a stream bound to a file or
    a borrowed descriptor has no such pipe, so the combination is contradictory
    rather than merely redundant.
    """
    with pytest.raises(ValueError, match="stdout"):
        c.RunOutputOptions(capture=True, stdout=c.StdioTarget.path(Path("o.log")))
    with pytest.raises(ValueError, match="stderr"):
        c.RunOutputOptions(capture=True, stderr=c.StdioTarget.inherit())


def test_output_options_accept_a_target_with_capture_disabled() -> None:
    """The near-miss case: the same target is valid once capture is off."""
    options = c.RunOutputOptions(capture=False, stdout=c.StdioTarget.path(Path("o")))
    assert options.stdout is not None, "the target must be retained"


def test_output_options_reject_one_path_for_both_streams() -> None:
    """Sharing a path between stdout and stderr is rejected at construction.

    Both streams would interleave into one descriptor opened twice, so the
    bytes land in the file in an order neither stream owns, and each open
    appends at its own offset.
    """
    shared = c.StdioTarget.path(Path("both.log"))
    with pytest.raises(ValueError, match=r"stdout.*stderr|stderr.*stdout"):
        c.RunOutputOptions(capture=False, stdout=shared, stderr=shared)


def test_output_options_accept_distinct_paths_for_both_streams() -> None:
    """The near-miss case for the shared-path rule."""
    options = c.RunOutputOptions(
        capture=False,
        stdout=c.StdioTarget.path(Path("out.log")),
        stderr=c.StdioTarget.path(Path("err.log")),
    )
    assert options.stdout != options.stderr, "distinct paths must stay distinct"


def test_output_options_reject_a_lines_redirect() -> None:
    """``lines()`` needs stdout to be a pipe, so a target is rejected there.

    ``RunOutputOptions`` cannot make this call on its own: the same object is
    perfectly valid for ``run()``, which has no line iterator to starve. The
    rejection therefore belongs to ``SafeCmd.lines``, where the requirement is
    actually known, and this pins that placement.
    """
    from cuprum import sh

    builder = sh.make(c.ECHO, catalogue=c.DEFAULT_CATALOGUE)
    redirect = c.RunOutputOptions(
        capture=False,
        stdout=c.StdioTarget.path(Path("lines.log")),
    )
    assert redirect.stdout is not None, "the target must be valid for run()"
    with pytest.raises(ValueError, match="stdout"):
        builder("hi").lines(output=redirect)
