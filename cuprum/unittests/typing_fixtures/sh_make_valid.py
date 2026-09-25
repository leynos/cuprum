"""Static-check fixture proving valid ``sh.make`` calls type-check.

This module is not imported or executed. The repository-wide ``make typecheck``
run walks ``cuprum/unittests`` and checks it as ordinary source, so it fails
the gate if the published ``SafeCmdBuilder`` contract stops accepting any call
written here. Only public names are imported, and every call sits inside a
function so the module has no import-time side effects.

The negative half of the contract is deliberately absent: a snippet the
checker is supposed to reject cannot live here without breaking the gate, so
``cuprum/unittests/test_sh_typing_contract.py`` writes those cases to a
temporary directory and requires ty to reject them.
"""

from __future__ import annotations

import sys
import typing as typ
from pathlib import Path

from cuprum import (
    ECHO,
    ArgValue,
    Program,
    ProgramCatalogue,
    SafeCmd,
    SafeCmdBuilder,
    sh,
)

#: A builder for a program curated by the default catalogue.
_ECHO_BUILDER = sh.make(ECHO)

#: A builder for the running interpreter, via an explicit catalogue.
_PYTHON_CATALOGUE = ProgramCatalogue.from_programs(sys.executable, name="typing")
_PYTHON_BUILDER = sh.make(Program(sys.executable), catalogue=_PYTHON_CATALOGUE)


def make_returns_the_builder_protocol() -> None:
    """``sh.make`` returns the protocol, not a bare callable."""
    typ.assert_type(sh.make(ECHO), SafeCmdBuilder)


def builder_call_returns_a_safe_cmd() -> None:
    """Yield a ``SafeCmd`` from a builder call."""
    typ.assert_type(_ECHO_BUILDER("hello"), SafeCmd)
    typ.assert_type(_PYTHON_BUILDER("-c", "print(1)"), SafeCmd)


def positional_arguments_accept_every_arg_value() -> None:
    """Each member of ``ArgValue`` is accepted positionally."""
    typ.assert_type(_ECHO_BUILDER("text"), SafeCmd)
    typ.assert_type(_ECHO_BUILDER(1), SafeCmd)
    typ.assert_type(_ECHO_BUILDER(1.5), SafeCmd)
    # Boolean positional acceptance is the contract under test.
    typ.assert_type(_ECHO_BUILDER(True), SafeCmd)  # ruff: ignore[boolean-positional-value-in-call] - value under test
    typ.assert_type(_ECHO_BUILDER(Path("example/status")), SafeCmd)


def keyword_values_accept_every_arg_value() -> None:
    """Keyword flag values carry the same domain as positional ones."""
    typ.assert_type(_ECHO_BUILDER(text="value"), SafeCmd)
    typ.assert_type(_ECHO_BUILDER(count=1), SafeCmd)
    typ.assert_type(_ECHO_BUILDER(ratio=1.5), SafeCmd)
    typ.assert_type(_ECHO_BUILDER(flag=True), SafeCmd)
    typ.assert_type(_ECHO_BUILDER(path=Path("example/status")), SafeCmd)


def boolean_flags_carry_both_values() -> None:
    """Carry both keyword boolean polarities through the builder."""
    typ.assert_type(_ECHO_BUILDER(porcelain=True), SafeCmd)
    typ.assert_type(_ECHO_BUILDER(porcelain=False), SafeCmd)


def path_like_values_are_accepted() -> None:
    """``Path`` is accepted positionally and as a keyword value."""
    typ.assert_type(_ECHO_BUILDER(Path("relative/path")), SafeCmd)
    typ.assert_type(_ECHO_BUILDER("example/status"), SafeCmd)
    typ.assert_type(_ECHO_BUILDER(destination=Path("example/status")), SafeCmd)


def explicit_argument_ordering_preserves_types() -> None:
    """Keep each argument in its own position in a mixed call."""
    # Boolean positional acceptance is the contract under test.
    typ.assert_type(_ECHO_BUILDER("first", 2, 3.5, True, Path("e/p")), SafeCmd)  # ruff: ignore[boolean-positional-value-in-call] - value under test


def unpacked_arg_sequences_are_accepted() -> None:
    """Accept an unpacked ``list[ArgValue]`` as positional arguments."""
    positional: list[ArgValue] = ["status", 1, 1.5, True, Path("example/status")]
    typ.assert_type(_ECHO_BUILDER(*positional), SafeCmd)


def unpacked_arg_mappings_are_accepted() -> None:
    """Accept an unpacked ``dict[str, ArgValue]`` as keyword arguments."""
    keywords: dict[str, ArgValue] = {"porcelain": True, "count": 3}
    typ.assert_type(_ECHO_BUILDER(**keywords), SafeCmd)


def a_builder_satisfies_a_protocol_parameter() -> None:
    """``SafeCmdBuilder`` works as a parameter annotation for consumers."""
    typ.assert_type(_accepts_builder(_ECHO_BUILDER), SafeCmd)


def _accepts_builder(builder: SafeCmdBuilder) -> SafeCmd:
    """Call a builder supplied through the protocol parameter."""
    return builder("passed-through")
