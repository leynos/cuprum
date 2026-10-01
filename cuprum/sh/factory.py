"""The ``make`` factory that turns a curated ``Program`` into a builder.

Part of the ``cuprum.sh`` package, which re-exports ``make``. The builder it
returns validates the program against a catalogue once, then coerces each
call's arguments into an immutable :class:`~cuprum.sh.safe_cmd.SafeCmd`.

Every keyword the builder accepts becomes a ``--flag=value`` child argument,
so the names that a caller is most likely to mean as execution settings are
reserved instead: the :class:`~cuprum.sh.execution.ExecutionContext` fields
and ``run_sync``'s own keyword-only parameters raise :class:`TypeError` rather
than being forwarded to the child. A tool whose command line genuinely takes
such a flag still receives it as a positional ``"--cwd=..."`` argument.
"""

# No ``from __future__ import annotations`` here: ``make``'s public signature is
# introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and ``Program`` and ``ProgramCatalogue`` are genuine runtime imports.
import dataclasses as dc

from cuprum.catalogue import DEFAULT_CATALOGUE, ProgramCatalogue
from cuprum.program import Program
from cuprum.sh.argv import _ArgValue, build_argv
from cuprum.sh.execution import ExecutionContext
from cuprum.sh.safe_cmd import SafeCmd, SafeCmdBuilder

__all__ = ["make"]

# Derived from the dataclass so a new execution-context field cannot silently
# become a child flag.
_CONTEXT_OPTIONS = frozenset(field.name for field in dc.fields(ExecutionContext))

# ``run_sync``'s own keyword-only parameters, which it consumes rather than
# forwards. Every ``run``/``lines`` parameter is either shared with ``run_sync``
# or one of the context fields above; ``test_sh`` guards that the two sets stay
# a superset of the builder's reserved names.
_RUN_OPTIONS = frozenset({"output", "timeout", "context", "stdin"})

_RESERVED_OPTIONS = _CONTEXT_OPTIONS | _RUN_OPTIONS


def _reserved_option_message(name: str) -> str:
    """Describe the correct ``run_sync`` spelling for a reserved keyword."""
    # ``run_sync``'s own parameters are named directly; context fields are
    # named through ``ExecutionContext`` so the caller sees where they go.
    if name in _RUN_OPTIONS:
        return f"{name} is an execution option; pass {name}=... to run_sync"
    return (
        f"{name} is an execution option; pass ExecutionContext({name}=...) to run_sync"
    )


def _reject_reserved_options(kwargs: dict[str, _ArgValue]) -> None:
    """Reject the first keyword naming an execution option, in insertion order."""
    for name in kwargs:
        if name in _RESERVED_OPTIONS:
            msg = _reserved_option_message(name)
            raise TypeError(msg)


def make(
    program: Program,
    *,
    catalogue: ProgramCatalogue = DEFAULT_CATALOGUE,
) -> SafeCmdBuilder:
    """Build a callable that produces ``SafeCmd`` instances for ``program``.

    Parameters
    ----------
    program : Program
        The program the built ``SafeCmd`` instances invoke; it must exist in
        ``catalogue``.
    catalogue : ProgramCatalogue
        The catalogue used to validate ``program`` and resolve its entry.

    Returns
    -------
    SafeCmdBuilder
        A callable that builds ``SafeCmd`` instances for ``program``.

    Raises
    ------
    UnknownProgramError
        If ``program`` does not exist in ``catalogue``.
    """  # ruff: ignore[docstring-extraneous-exception] - UnknownProgramError propagates from catalogue.lookup
    entry = catalogue.lookup(program)

    def builder(*args: _ArgValue, **kwargs: _ArgValue) -> SafeCmd:
        """Coerce ``args``/``kwargs`` into a ``SafeCmd`` for the program.

        Returns
        -------
        SafeCmd
            The command carrying the catalogue entry's program and project.

        Raises
        ------
        TypeError
            If a keyword names an execution option rather than a child flag.
        """  # ruff: ignore[docstring-extraneous-exception] - TypeError propagates from _reject_reserved_options
        _reject_reserved_options(kwargs)
        argv = build_argv(*args, **kwargs)
        return SafeCmd(program=entry.program, argv=argv, project=entry.project)

    return builder
