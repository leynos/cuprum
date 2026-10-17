"""Typed and validated settings for the GitHub Actions sink."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.redaction import RedactionPolicy


class _GitHubActionsSinkOptionsKwargs(typ.TypedDict, total=False):
    """Keyword-compatible settings accepted by ``GitHubActionsSink``."""

    title: str | None
    force: bool
    emit_group: bool
    emit_annotation: bool
    redaction: RedactionPolicy | None


@dc.dataclass(frozen=True, slots=True)
class _GitHubActionsSinkOptions:
    """Normalized immutable settings for one GitHub Actions sink."""

    title: str | None = None
    force: bool = False
    emit_group: bool = True
    emit_annotation: bool = True
    redaction: RedactionPolicy | None = None


def _normalize_github_actions_sink_options(
    options: _GitHubActionsSinkOptionsKwargs,
) -> _GitHubActionsSinkOptions:
    """Build immutable options and validate the workflow command toggles."""
    settings = _GitHubActionsSinkOptions(**options)
    _reject_non_bool("emit_group", settings.emit_group)
    _reject_non_bool("emit_annotation", settings.emit_annotation)
    return settings


def _reject_non_bool(name: str, value: object) -> None:
    """Reject a value that is not the documented ``bool`` for a toggle."""
    if not isinstance(value, bool):
        msg = f"{name} must be a bool, got {value!r}"
        raise TypeError(msg)
