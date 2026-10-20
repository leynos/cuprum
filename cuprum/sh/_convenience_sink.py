"""Synthesize presentation sinks from run-output convenience flags.

``RunOutputOptions`` exposes two opt-in booleans — ``group`` and
``annotate_failure`` — that are not destinations of their own but spellings of
one: each synthesizes a :class:`~cuprum.sinks.GitHubActionsSink` configured for
its half of the framing, so the workflow-command syntax, the stop-commands
lease, and the environment gate all stay inside the adapter and the execution
layer learns nothing new.

Sink synthesis lives here rather than beside the dataclass because it describes
what the flags *produce* rather than what the options *are*. Validation lives
with the other output-field rules in ``cuprum.sh.output_rules``.

The identity metadata that ties a synthesized sink back to the options object
that made it — and so lets :func:`dataclasses.replace` resynthesize the sink
when a flag changes while leaving a caller-supplied sink alone — is the
``_synthesized_sink`` field on the options class, which this module reads and
writes but does not declare.
"""

from __future__ import annotations

import typing as typ

# ``GitHubActionsSink`` comes from the same package surface rather than its
# ``github_actions`` submodule: the package publishes it in ``__all__``, and the
# convenience flags synthesize it as part of the options class's documented
# contract.
from cuprum.sinks import GitHubActionsSink

if typ.TYPE_CHECKING:
    from cuprum.sh.output import RunOutputOptions

__all__ = [
    "synthesize_sink",
]


def synthesize_sink(options: RunOutputOptions) -> None:
    """Materialize the convenience flags as this run's presentation sink.

    Both flags are spelled as a configuration of the existing
    :class:`~cuprum.sinks.GitHubActionsSink` rather than as new framing
    code, which is what keeps workflow-command syntax inside the adapter.
    The synthesized sink carries no ``force``, so it stays subject to the
    adapter's own ``GITHUB_ACTIONS`` gate. Private identity metadata tracks
    the adapter created by this options object so
    :func:`dataclasses.replace` can resynthesize it when flags change.
    An adapter supplied to a new options object remains explicit regardless
    of its concrete type.
    """
    is_synthesized_sink = options.sink is options._synthesized_sink
    if options.sink is not None and not is_synthesized_sink:
        object.__setattr__(options, "_synthesized_sink", None)
        return
    if not (options.group or options.annotate_failure):
        if is_synthesized_sink:
            object.__setattr__(options, "sink", None)
        object.__setattr__(options, "_synthesized_sink", None)
        return
    synthesized_sink = GitHubActionsSink(
        emit_group=options.group,
        emit_annotation=options.annotate_failure,
    )
    object.__setattr__(
        options,
        "sink",
        synthesized_sink,
    )
    object.__setattr__(options, "_synthesized_sink", synthesized_sink)
