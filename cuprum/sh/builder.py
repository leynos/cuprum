"""The public callable contract for ``sh.make`` builders.

``sh.make`` returns a :class:`SafeCmdBuilder` rather than a bare
``Callable[..., SafeCmd]``. The ellipsis in the old annotation hid the
accepted argument domain from static checkers: a call passing ``object()``
type-checked cleanly, and the runtime stringified its ``repr`` straight onto
the command line. The protocol below states the positional and keyword
parameters so both halves of that contract agree.

The ``cuprum.sh`` package re-exports ``SafeCmdBuilder``.
"""

# No ``from __future__ import annotations`` here: ``ArgValue`` and ``SafeCmd``
# are genuine runtime imports so the protocol's signatures stay introspectable
# with ``typing.get_type_hints``.
import typing as typ

from cuprum.sh.argv import ArgValue
from cuprum.sh.safe_cmd import SafeCmd

__all__ = ["SafeCmdBuilder"]


class SafeCmdBuilder(typ.Protocol):
    """Structural contract for the callable ``sh.make`` returns.

    The protocol is deliberately not decorated with
    :func:`typing.runtime_checkable`. It exists to describe a call signature
    to static checkers, and an ``isinstance`` test against a protocol whose
    only method is ``__call__`` would confirm little beyond the object being
    callable at all.
    """

    def __call__(self, *args: ArgValue, **kwargs: ArgValue) -> SafeCmd:
        """Build a command for the program the builder was created from.

        Parameters
        ----------
        *args
            Positional argument values, stringified with ``str()`` in the
            order supplied.
        **kwargs
            Keyword flag values, serialized after the positionals as
            ``--flag=value``, with underscores in each name replaced by
            hyphens.

        Returns
        -------
        SafeCmd
            The command carrying the builder's program and project metadata.

        Raises
        ------
        TypeError
            If any value is ``None``, or is not a ``str``, ``int``,
            ``float``, ``bool``, or :class:`pathlib.Path`.

        """
        raise NotImplementedError
