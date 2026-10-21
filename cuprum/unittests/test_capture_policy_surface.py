"""Unit tests for the public capture-policy surface introduced by issue #443.

A caller configures capture per stream by naming a policy type, so those types
are part of the public surface of both ``cuprum`` and ``cuprum.sh``. These
tests pin that export contract, and pin the two runtime ends it promises: a
run that names no policy still returns the text it always returned, and a run
that disables capture reports neither text nor records.
"""

from __future__ import annotations

import pytest

import cuprum as c
from cuprum import ScopeConfig, scoped
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import build_python_catalogue_env

# Distinct per stream, and unequal to each other, so a run that swapped the two
# pipes or reported one stream's text under the other's name fails the default
# case below rather than satisfying both of its assertions with a single value.
_STDOUT_TEXT = "default stdout"
_STDERR_TEXT = "default stderr"
_ECHO_BOTH_SOURCE = (
    f"import sys; sys.stdout.write({_STDOUT_TEXT!r}); "
    f"sys.stderr.write({_STDERR_TEXT!r})"
)


@pytest.mark.parametrize(
    "name",
    [
        "FullCapture",
        "BoundedCapture",
        "FileBackedCapture",
        "CapturedStream",
        "CapturePolicyKind",
    ],
)
def test_capture_policy_types_are_public_surface(name: str) -> None:
    """Every capture type a caller names is exported from both packages.

    Issue #443's contract is that a caller configures capture by naming a
    policy, so each policy type has to be reachable from ``cuprum`` and
    ``cuprum.sh`` rather than only from the module that defines it.
    """
    assert name in c.__all__, f"cuprum.__all__ must export {name}"
    assert name in c.sh.__all__, f"cuprum.sh.__all__ must export {name}"
    assert hasattr(c, name), f"cuprum must expose {name}"
    assert hasattr(c.sh, name), f"cuprum.sh must expose {name}"
    assert getattr(c, name) is getattr(c.sh, name), (
        f"both packages must expose the same {name}, not two copies"
    )


def test_a_default_run_still_returns_complete_text_under_full_capture() -> None:
    """Opting out entirely is the only thing that changes what a caller reads.

    This is the runtime half of the compatibility claim. ``RunOutputOptions()``
    resolves both streams to :class:`FullCapture`, so a run that names no policy
    returns exactly the ``stdout``/``stderr`` text the surface always returned —
    nothing bounded, nothing spilled, nothing absent. The records that come with
    it say ``full`` and report ``memory``, which is what makes the addition
    additive: a caller who never reads ``captures`` is unaffected by it.

    The companion case below covers the configuration that *does* remove text,
    so the pair pins both ends rather than only the reassuring one. The child
    writes a distinct value to each stream, so a run that transposed the two
    pipes fails here instead of satisfying both assertions with one value.
    """
    options = RunOutputOptions()
    assert options.resolved_capture == (c.FullCapture(), c.FullCapture()), (
        "the default must resolve both streams to full capture; got "
        f"{options.resolved_capture!r}"
    )

    result = _run_echoing_child(options)

    assert result.stdout == _STDOUT_TEXT, (
        f"the default must still return stdout text; got {result.stdout!r}"
    )
    assert result.stderr == _STDERR_TEXT, (
        f"the default must still return stderr text; got {result.stderr!r}"
    )
    assert [(record.stream, record.policy) for record in result.captures] == [
        ("stdout", "full"),
        ("stderr", "full"),
    ], (
        "the default's records must describe full memory capture for both "
        f"streams; got {result.captures!r}"
    )
    assert all(record.storage == "memory" for record in result.captures), (
        "a default run must not spill; every record must report memory storage"
    )


def test_a_run_that_disables_capture_reports_neither_text_nor_records() -> None:
    """The one configuration that withholds text withholds records with it.

    ``capture=False`` is the pre-existing way for a caller to say they want
    nothing, and it must not gain an empty record per stream as the price of the
    new field: a run that captured nothing reports nothing, so a caller
    iterating ``captures`` sees exactly the streams that were captured.
    """
    options = RunOutputOptions(capture=False)
    assert options.resolved_capture == (None, None), (
        f"capture=False must resolve both streams to no policy; got "
        f"{options.resolved_capture!r}"
    )

    result = _run_echoing_child(options)

    assert result.captures == (), (
        "a run that captured no stream must report no capture records; "
        f"got {result.captures!r}"
    )
    assert result.stdout is None, (
        f"a run that captured no stdout must report None; got {result.stdout!r}"
    )
    assert result.stderr is None, (
        f"a run that captured no stderr must report None; got {result.stderr!r}"
    )


def _run_echoing_child(options: c.RunOutputOptions) -> c.CommandResult:
    """Run a child that writes a distinct value to each stream.

    Returns
    -------
    c.CommandResult
        The completed run, whose text and capture records the caller inspects.
    """
    env = build_python_catalogue_env()
    with scoped(ScopeConfig(allowlist=frozenset({env.program}))):
        return env.builder("-c", _ECHO_BOTH_SOURCE).run_sync(output=options)
