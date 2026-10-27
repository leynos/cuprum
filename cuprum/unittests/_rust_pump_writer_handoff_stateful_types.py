"""Typed stage and transition descriptions for the writer-handoff model."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type _Stage = typ.Literal[
    "new",
    "duplicated",
    "blocking_ready",
    "submitted",
    "native_loaded",
    "buffer_validated",
    "platform_ready",
    "native_finished",
    "worker_finished",
    "settled",
    "restored",
    "resumed",
    "complete",
    "pre_submission_failed",
]


class _Transition(typ.NamedTuple):
    """Describe one enabled operation without filtering invalid rule draws."""

    name: str
    apply: cabc.Callable[[], None]
