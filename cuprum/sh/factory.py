"""The ``make`` factory that turns a curated ``Program`` into a builder.

Part of the ``cuprum.sh`` package, which re-exports ``make``. The builder it
returns validates the program against a catalogue once, then coerces each
call's arguments into an immutable :class:`~cuprum.sh.safe_cmd.SafeCmd`.
"""

# No ``from __future__ import annotations`` here: ``make``'s public signature is
# introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and ``Program`` and ``ProgramCatalogue`` are genuine runtime imports.
from cuprum.catalogue import DEFAULT_CATALOGUE, ProgramCatalogue
from cuprum.program import Program
from cuprum.sh.argv import ArgValue, build_argv
from cuprum.sh.builder import SafeCmdBuilder
from cuprum.sh.safe_cmd import SafeCmd

__all__ = ["make"]


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
        A callable that builds ``SafeCmd`` instances for ``program``. It
        accepts the same positional and keyword values the runtime validates:
        ``str``, ``int``, ``float``, ``bool``, or :class:`pathlib.Path`.

    Raises
    ------
    UnknownProgramError
        If ``program`` does not exist in ``catalogue``.
    """  # ruff: ignore[docstring-extraneous-exception] - UnknownProgramError propagates from catalogue.lookup
    entry = catalogue.lookup(program)

    def builder(*args: ArgValue, **kwargs: ArgValue) -> SafeCmd:
        """Coerce ``args``/``kwargs`` into a ``SafeCmd`` for the program."""
        argv = build_argv(*args, **kwargs)
        return SafeCmd(program=entry.program, argv=argv, project=entry.project)

    return builder
