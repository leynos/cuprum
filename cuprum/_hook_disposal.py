"""Shared disposal of values returned by synchronous observation hooks.

The pump, echo, and stream-operation channels all close native coroutines
returned by synchronous hooks. This module keeps that boundary and its
ordinary-exception policy in one place while each channel supplies its own
logger, action, and event context.
"""

from __future__ import annotations

import dataclasses as dc
import inspect
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import logging


@dc.dataclass(frozen=True, slots=True)
class _HookDisposalContext:
    """Channel-specific logging data for a returned-coroutine disposal."""

    logger: logging.Logger
    action: str
    message: str
    message_args: tuple[object, ...]
    extra_fields: cabc.Mapping[str, object]


def _close_returned_coroutine(
    result: object,
    context: _HookDisposalContext,
) -> None:
    """Close a native coroutine and absorb ordinary disposal failures."""
    if not inspect.iscoroutine(result):
        return
    try:
        result.close()
    except Exception as exc:
        error_type = type(exc).__name__
        context.logger.warning(
            context.message,
            *context.message_args,
            error_type,
            exc_info=True,
            extra={
                "cuprum_action": context.action,
                **context.extra_fields,
                "cuprum_error_type": error_type,
            },
        )
