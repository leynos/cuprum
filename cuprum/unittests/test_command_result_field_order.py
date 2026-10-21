"""Unit tests pinning ``CommandResult``'s public positional field order.

``CommandResult`` is a public, non-``kw_only`` dataclass, so its first seven
fields are positional and the measurements after them are keyword-only. These
tests pin that split: a field inserted ahead of ``relay_fallbacks`` silently
rebinds every seven-argument construction, and a measurement that lost
``kw_only`` would absorb an eighth argument with no runtime error.
"""

from __future__ import annotations

import dataclasses as dc

import pytest

import cuprum as c


def test_command_result_keeps_relay_fallbacks_as_its_trailing_slot() -> None:
    """``relay_fallbacks`` must stay the seventh positional CommandResult field.

    ``CommandResult`` is a public, non-``kw_only`` dataclass whose first seven
    fields are positional. Keeping ``relay_fallbacks`` seventh preserves the
    positional contract main established; the later measurement fields are
    keyword-only, so they cannot take a positional slot ahead of it. Inserting a
    positional field earlier silently rebinds a seven-argument construction --
    the relay tuple lands on the new field and ``relay_fallbacks`` falls back to
    ``()`` -- with no runtime error, because Python does not check argument
    types. This test pins that ordering and the keyword-only measurements.
    """
    fields = [f.name for f in dc.fields(c.CommandResult)]
    # ``kw_only`` moves a field out of the generated ``__init__``'s positional
    # order, not out of ``dc.fields()``, so the call contract is read off the
    # positional projection rather than the declaration index.
    positional = [f.name for f in dc.fields(c.CommandResult) if not f.kw_only]
    assert positional[6] == "relay_fallbacks", (
        "relay_fallbacks must be the seventh positional slot so existing "
        "seven-argument callers keep binding it; the measurement fields are "
        f"keyword-only and must never take a slot, got positional={positional}"
    )
    assert positional[-1] == "relay_fallbacks", (
        "relay_fallbacks must be the last positional field, so the generated "
        f"signature keeps it trailing, got positional={positional}"
    )
    # ``captures`` is declared after ``relay_fallbacks`` and is keyword-only,
    # so it may take the declaration tail but never the positional one. Pinning
    # both together is what stops a later field from quietly becoming
    # positional and rebinding the seventh argument.
    assert fields[-1] == "captures", (
        "captures must be declared last as the additive tail; it is "
        f"keyword-only and takes no positional slot, got {fields}"
    )
    assert "captures" not in positional, (
        f"captures must stay keyword-only, got positional={positional}"
    )
    result = c.CommandResult(
        c.Program(c.ECHO),  # program
        (),  # argv
        0,  # exit_code
        4242,  # pid
        "out",  # stdout
        "err",  # stderr
    )
    assert result.stdout == "out", (
        f"stdout must bind positionally, got {result.stdout!r}"
    )
    assert result.stderr == "err", (
        f"stderr must bind positionally, got {result.stderr!r}"
    )
    assert not result.relay_fallbacks, (
        f"the defaulted diagnostics must be empty, got {result.relay_fallbacks!r}"
    )

    sentinel = c.RelayFallback(
        stream=c.EchoStream.STDOUT,
        error_category=c.EchoErrorCategory.UNICODE_ENCODE,
    )
    relayed = c.CommandResult(c.ECHO, (), 0, 1, "out", "err", (sentinel,))
    assert relayed.relay_fallbacks == (sentinel,), (
        "a seventh positional argument must bind relay_fallbacks, got "
        f"{relayed.relay_fallbacks!r}"
    )
    assert relayed.started_at == pytest.approx(0.0), (
        "the measurements must not be reachable positionally, got "
        f"started_at={relayed.started_at!r}"
    )

    _assert_measurements_are_keyword_only(sentinel)


def _assert_measurements_are_keyword_only(sentinel: c.RelayFallback) -> None:
    """Reject an eighth positional argument rather than binding a measurement.

    The measurements are keyword-only, so they can never take an eighth
    positional slot and silently absorb a call with too many arguments. The
    arity is asserted at runtime on purpose: if a later change drops
    ``kw_only``, the eighth argument would bind ``started_at`` with no error,
    and this is the assertion that catches it. The call is unpacked and its
    static rejection suppressed for the same reason -- a type checker refuses
    the call before it can be made, which is exactly the failure under test.
    """
    over_long: tuple[object, ...] = (
        c.ECHO,
        (),
        0,
        1,
        "out",
        "err",
        (sentinel,),
        0.0,
    )
    with pytest.raises(TypeError):
        c.CommandResult(*over_long)  # ty: ignore[too-many-positional-arguments] - the over-arity call is the assertion
