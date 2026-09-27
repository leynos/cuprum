"""Validation-rule tests for the standard-stream vocabulary.

``test_safe_cmd_redirect.py`` proves redirection *works*: bytes land in the
file cuprum opened, a borrowed descriptor survives the run, an owned one is
closed. This module covers the other half of the contract — the combinations
cuprum refuses, and the legal combination sitting right beside each refusal.

Every rejection is paired with an accepted near-miss, because a rule that
refused everything would satisfy the rejection assertions on its own. The
pairings are what make these rows evidence of a *boundary*:

- A :class:`StdioTarget` variant carrying the wrong payload is refused, while
  the correctly shaped variant of the same kind builds and reads back.
- ``RunOutputOptions.stdin`` refuses a ``path`` or ``fd`` target, while
  ``stdout`` accepts the very same target once capture is off — so the rule is
  scoped to the stream, not to the variant.
- stdout and stderr refuse to share one owned path, while two ``fd`` targets
  naming one descriptor are the caller's own arrangement and are allowed.
- ``SafeCmd.lines()`` refuses a redirected stdout, while the same stream named
  explicitly as a pipe iterates as usual.

Only the last pair needs a child; everything else is decided at construction,
which is where the vocabulary's whole contract lives. That is deliberate: the
rules exist so a contradictory combination fails immediately rather than as a
hung read or a silently discarded stream, and a test that spawned a process
first would not be able to tell those apart.
"""

from __future__ import annotations

import asyncio
import re
import typing as typ

import pytest

from cuprum.sh import RunOutputOptions, StdioTarget
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmd


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


# The message a rejected combination must carry. Held as the literal text, not
# pre-escaped: ``_rejects`` escapes every expectation before handing it to
# ``pytest.raises(match=...)``, because the messages quote kinds, paths, and a
# sorted set, so several of them contain characters the ``match`` argument
# would otherwise read as regex syntax.
_KIND_MENU = "StdioTarget kind must be one of ['fd', 'inherit', 'path', 'pipe']"


def _rejects(call: cabc.Callable[[], object], expected: str) -> str:
    """Run *call*, require its ``ValueError``, and return the message.

    Returns
    -------
    str
        The rejection message, so a case can assert on wording the pattern
        alone would not pin.
    """
    with pytest.raises(ValueError, match=re.escape(expected)) as info:
        call()
    return str(info.value)


@pytest.mark.parametrize(
    ("target_call", "expected"),
    [
        # Each row names the variant's own payload requirement: the *wrong*
        # shape for that kind, not an arbitrary bad value. The rows that pass
        # a value the vocabulary does not define carry a ``ty: ignore`` naming
        # that intent, because refusing such a value at runtime is exactly the
        # behaviour under test.
        (
            lambda: StdioTarget(kind="nonsense"),  # ty: ignore[invalid-argument-type]
            _KIND_MENU,
        ),
        (
            lambda: StdioTarget(kind="path", value=None),
            "StdioTarget.path requires a path",
        ),
        (
            lambda: StdioTarget(kind="path", value=3),
            "StdioTarget.path requires a path",
        ),
        (
            lambda: StdioTarget(kind="fd", value=None),
            "StdioTarget.fd requires a descriptor or file object",
        ),
        (
            lambda: StdioTarget(kind="pipe", value=1),
            "StdioTarget.pipe() takes no payload",
        ),
        (
            lambda: StdioTarget(
                kind="inherit",
                value="somewhere",  # ty: ignore[invalid-argument-type]
            ),
            "StdioTarget.inherit() takes no payload",
        ),
    ],
)
def test_stdio_target_rejects_a_variant_with_the_wrong_payload(
    target_call: cabc.Callable[[], object],
    expected: str,
) -> None:
    """A variant carrying the wrong shape is refused, naming the requirement."""
    message = _rejects(target_call, expected)

    assert expected in message, (
        f"the rejection must name the variant's requirement; got {message!r}"
    )


def test_every_variant_builds_with_its_own_payload(
    tmp_path: Path,
) -> None:
    """The near-miss for the payload rows: each shape is accepted as itself.

    Without this, an implementation that refused every construction would pass
    every row above. Each accessor is also the only way to read a path or
    descriptor back out, so reading them here is what proves the accepted
    value survived construction rather than being stored unnormalized.
    """
    owned = StdioTarget.path(tmp_path / "log.txt")
    borrowed = StdioTarget.fd(7)

    assert owned.kind == "path", "a path target must name its own kind"
    assert owned.is_owned_path, "a path target must report that cuprum owns the file"
    assert owned.path_value == tmp_path / "log.txt", (
        "a path target must accept a str or Path and normalize to Path"
    )
    assert StdioTarget.path(str(tmp_path / "log.txt")) == owned, (
        "the str and Path spellings must build equal targets"
    )
    assert borrowed.fd_value == 7, "a descriptor target must read back its fd"
    assert not borrowed.is_owned_path, "a borrowed descriptor is not cuprum's to close"
    assert StdioTarget.pipe().value is None, "a pipe target carries no payload"
    assert StdioTarget.inherit().value is None, "an inherit target carries no payload"


@pytest.mark.parametrize("kind", ["path", "fd"])
def test_stdin_rejects_a_destination_rather_than_an_input(
    kind: str,
    tmp_path: Path,
) -> None:
    """Stdin accepts only the variants that describe the stream itself.

    A destination is not an input: the bytes have to come from somewhere, and
    ``SafeCmd.run``'s ``stdin=`` argument is where they come from. Accepting a
    second, competing spelling here would leave one of the two silently
    ignored.
    """
    target = (
        StdioTarget.path(tmp_path / "in.txt") if kind == "path" else StdioTarget.fd(0)
    )

    message = _rejects(
        lambda: RunOutputOptions(capture=False, stdin=target),
        "RunOutputOptions stdin cannot be redirected to",
    )

    assert repr(kind) in message, (
        f"the rejection must name the offending kind; got {message!r}"
    )
    assert "stdin=StdinInput(...)" in message, (
        f"the rejection must point at the StdinInput spelling; got {message!r}"
    )
    assert "stdin=StdinStream(...)" in message, (
        f"the rejection must point at the StdinStream spelling; got {message!r}"
    )


def test_the_same_path_target_is_accepted_for_stdout(tmp_path: Path) -> None:
    """The near-miss for the stdin rule: the variant is scoped, not banned.

    ``StdioTarget.path`` is refused as a *source* of stdin and accepted as a
    destination for stdout, which is exactly the asymmetry the rule encodes.
    An implementation that rejected the variant outright would fail here.
    """
    options = RunOutputOptions(capture=False, stdout=StdioTarget.path(tmp_path / "o"))

    assert options.stdout is not None, "the target must survive construction"
    assert options.stdout.is_owned_path, (
        "a path target must be accepted for a stream that cuprum writes"
    )


@pytest.mark.parametrize("kind", ["pipe", "inherit"])
def test_stdin_accepts_both_stream_describing_variants(
    kind: typ.Literal["pipe", "inherit"],
) -> None:
    """The two accepted stdin variants stay accepted, with capture left alone."""
    options = RunOutputOptions(stdin=StdioTarget(kind=kind))

    assert options.stdin is not None, "the target must survive construction"
    assert options.stdin.kind == kind, (
        f"an explicit {kind} stdin target must keep its kind"
    )


def test_one_owned_path_cannot_serve_both_output_streams(tmp_path: Path) -> None:
    """Two owned opens of one file start at offset 0, so the streams collide."""
    shared = StdioTarget.path(tmp_path / "both.log")

    message = _rejects(
        lambda: RunOutputOptions(capture=False, stdout=shared, stderr=shared),
        "stdout and stderr cannot share one path",
    )

    assert "distinct paths" in message, (
        f"the rejection must name the supported path arrangement; got {message!r}"
    )
    assert "StdioTarget.fd" in message, (
        f"the rejection must name the borrowed-descriptor escape; got {message!r}"
    )


def test_distinct_paths_are_accepted(tmp_path: Path) -> None:
    """The near-miss: two *different* owned paths are the ordinary case.

    The runtime half of this pair — that the two files really do receive their
    own stream — is ``test_both_streams_can_be_redirected_at_once`` in
    ``test_safe_cmd_redirect.py``. All this case adds is that the rule keys on
    the path rather than on the presence of two ``path`` targets.
    """
    options = RunOutputOptions(
        capture=False,
        stdout=StdioTarget.path(tmp_path / "out.log"),
        stderr=StdioTarget.path(tmp_path / "err.log"),
    )

    assert options.stdout != options.stderr, (
        "two distinct paths must build distinct targets"
    )


def test_one_borrowed_descriptor_may_serve_both_streams() -> None:
    """A shared *borrowed* descriptor is the caller's arrangement, not cuprum's.

    Cuprum opens nothing here, so it cannot choose an offset to collide with,
    and the caller who shares one descriptor knows they share one offset. The
    rule therefore stays silent — which is what keeps the shared-path
    rejection from being a blanket refusal of shared destinations.
    """
    both = StdioTarget.fd(1)

    options = RunOutputOptions(capture=False, stdout=both, stderr=both)

    assert options.stdout == StdioTarget.fd(1) == options.stderr, (
        "one borrowed descriptor may name both streams"
    )


@pytest.mark.parametrize(
    ("options_call", "expected"),
    [
        # Capture and each echo flag are separate gates: the same target is
        # refused for the stream whose consumer needs a pipe and accepted for
        # the other one, so no single blank refusal can satisfy both rows.
        (
            lambda out: RunOutputOptions(capture=True, stdout=out),
            "stdout cannot be redirected",
        ),
        (
            lambda out: RunOutputOptions(capture=True, stderr=out),
            "stderr cannot be redirected",
        ),
        (
            lambda out: RunOutputOptions(capture=False, echo=True, stdout=out),
            "stdout cannot be redirected",
        ),
        (
            lambda out: RunOutputOptions(capture=False, echo_stderr=True, stderr=out),
            "stderr cannot be redirected",
        ),
    ],
)
def test_capture_or_echo_rejects_a_redirected_stream(
    options_call: cabc.Callable[[StdioTarget], RunOutputOptions],
    expected: str,
    tmp_path: Path,
) -> None:
    """A consumer that reads the parent's pipe cannot read a file instead."""
    out = StdioTarget.path(tmp_path / "redirected.log")

    message = _rejects(lambda: options_call(out), expected)

    assert "capture=False" in message, (
        f"the rejection must name the capture setting to relax; got {message!r}"
    )
    assert "echo=False" in message, (
        f"the rejection must name the echo setting to relax; got {message!r}"
    )


def test_capture_accepts_an_explicit_pipe_target() -> None:
    """The near-miss for the capture rows: a pipe target is what capture wants.

    The runtime half — that such a run really does capture — is
    ``test_pipe_target_still_captures`` in ``test_safe_cmd_redirect.py``. What
    this adds is that naming the pipe explicitly is legal while naming a file
    is not, so the rule is about the target and not about having set one.
    """
    options = RunOutputOptions(
        capture=True,
        echo=True,
        stdout=StdioTarget.pipe(),
        stderr=StdioTarget.pipe(),
    )

    assert options.capture, "capture must stay enabled with explicit pipe targets"
    assert options.resolved_echo == (True, True), (
        "echo must stay enabled for both streams with explicit pipe targets"
    )


def test_lines_rejects_a_redirected_stdout(tmp_path: Path) -> None:
    """Line iteration reads the parent's pipe, so a file leaves it nothing.

    ``RunOutputOptions`` cannot make this call by itself: the same object is a
    valid argument to ``run()``, which reads nothing back. The requirement
    belongs to ``lines()``, and this is the row that pins it there.
    """
    command = build_python_builder()("-c", "print('out')")
    log = tmp_path / "lines.log"
    options = RunOutputOptions(capture=False, stdout=StdioTarget.path(log))

    message = _rejects(
        lambda: command.lines(output=options),
        "SafeCmd.lines requires stdout to be a pipe",
    )

    assert "Use SafeCmd.run" in message, (
        f"the rejection must name the supported alternative; got {message!r}"
    )


def test_lines_iterates_with_an_explicit_pipe_target(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The near-miss: an explicit pipe is a pipe, and iteration proceeds.

    Paired with the rejection above, this shows the rule refuses the
    *destination* and not the act of naming one. ``capture=False`` keeps the
    case on the observation path, so the lines arrive because the pipe exists
    rather than because capture happened to open one.
    """

    async def collect() -> list[str]:
        """Iterate a child's lines through an explicitly named pipe."""
        stream = command.lines(
            output=RunOutputOptions(capture=False, stdout=StdioTarget.pipe())
        )
        return [event.text async for event in stream]

    command = python_builder("-c", "print('first'); print('second')")
    assert asyncio.run(collect()) == ["first", "second"], (
        "an explicitly piped stdout must still yield the child's lines"
    )
