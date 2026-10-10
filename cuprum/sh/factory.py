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
from cuprum.context import current_context
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


def _resolve_catalogue(catalogue: ProgramCatalogue | None) -> ProgramCatalogue:
    """Return the catalogue named explicitly, scoped or defaulted."""
    if catalogue is not None:
        return catalogue
    scoped_catalogue = current_context().catalogue
    if scoped_catalogue is not None:
        return scoped_catalogue
    return DEFAULT_CATALOGUE


def make(
    program: Program,
    *,
    catalogue: ProgramCatalogue | None = None,
) -> SafeCmdBuilder:
    """Build a callable that produces ``SafeCmd`` instances for ``program``.

    The catalogue is resolved once, when this function is called, in this
    order:

    1. an explicit ``catalogue`` argument;
    2. the innermost active ``scoped(catalogue=...)``;
    3. ``DEFAULT_CATALOGUE``, outside any catalogue scope.

    The resolved catalogue is bound to the returned builder, so the builder
    keeps working after the scope that supplied it exits. Passing a scope with
    only an allowlist (``ScopeConfig(allowlist=...)``) does not change which
    catalogue is active.

    Membership is checked here, at construction. Permission is a separate
    check: the allowlist of whichever context is active when the command runs
    decides whether it may execute. A command built from a catalogue the
    enclosing scope does not allow therefore constructs successfully and
    raises :class:`ForbiddenProgramError` at ``run`` or ``run_sync`` time.

    Parameters
    ----------
    program : Program
        The program the built ``SafeCmd`` instances invoke; it must exist in
        the resolved catalogue.
    catalogue : ProgramCatalogue | None, optional
        The catalogue used to validate ``program`` and resolve its entry.
        Defaults to the innermost scoped catalogue, then
        ``DEFAULT_CATALOGUE``.

    Returns
    -------
    SafeCmdBuilder
        A callable that builds ``SafeCmd`` instances for ``program``.

    Raises
    ------
    UnknownProgramError
        If ``program`` does not exist in the resolved catalogue.
    """  # ruff: ignore[docstring-extraneous-exception] - UnknownProgramError propagates from catalogue.lookup
    entry = _resolve_catalogue(catalogue).lookup(program)

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
