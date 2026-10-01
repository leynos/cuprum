"""The two unions a run's result may belong to, and nothing else.

A run reports either decoded text or the child's own bytes, so the four result
classes pair up into two unions:

- a single command reports a :class:`~cuprum.sh.CommandResult` or a
  :class:`~cuprum.sh.BytesCommandResult`;
- a pipeline reports a :class:`~cuprum.sh.PipelineResult` or a
  :class:`~cuprum.sh.BytesPipelineResult`.

:class:`BytesCommandResult` deliberately does not subclass
:class:`CommandResult` -- the two declare different field types for the same
names, so one cannot stand in for the other -- which means every helper that
handles *either* has to say ``A | B``. Naming the two unions once keeps that
spelling from being restated at each of them.

The module exists on its own, rather than beside the rules that build results
in :mod:`cuprum._result_assembly`, so that layers which must not import the
assembly rules can still name the unions. ``cuprum.context`` is the case that
forces the split: the hook signatures it publishes accept either result class,
and the assembly rules reach back into ``cuprum.context`` through
:mod:`cuprum._observability`, so importing them from there would close a cycle.

Every annotation here is resolved only by a type checker. The ``else`` arm
binds the names to ``object`` at runtime instead of leaving them unbound,
because the project requires public annotations to stay resolvable through
``typing.get_type_hints``: a reader that reaches one of these through an
importing module must get a value, not a ``NameError``.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.sh import (
        BytesCommandResult,
        BytesPipelineResult,
        CommandResult,
        PipelineResult,
    )

if typ.TYPE_CHECKING:
    type _AnyCommandResult = CommandResult | BytesCommandResult
    type _AnyPipelineResult = PipelineResult | BytesPipelineResult
else:
    _AnyCommandResult = object
    _AnyPipelineResult = object


__all__ = [
    "_AnyCommandResult",
    "_AnyPipelineResult",
]
