"""The environment-overlay registration handle and its factory.

The ``env`` registration is the one scope handle whose public surface is
large enough to carry its own module: the overlay-only policy needs more
explanation than the allowlist, hook, and binding handles put together,
because whether the live :func:`os.environ` participates depends on the
:class:`~cuprum.context.EnvMode` in effect.

Moved here from :mod:`cuprum.context.registration` to keep that module inside
the repository's 400-line ceiling; the names are re-exported there, so
``cuprum.context.registration.env`` and ``cuprum.context.env`` are unchanged.
"""

from __future__ import annotations

import typing as typ

from cuprum.context._registration_base import _TokenRegistration
from cuprum.context.env_overlay import (
    EnvMode,
    EnvOverlay,
    EnvOverlayValue,
    _coerce_env_overlay,
)
from cuprum.context.state import current_context

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["EnvRegistration", "env"]


class EnvRegistration(_TokenRegistration):
    """Registration handle for a scoped environment overlay.

    The overlay is layered on top of any overlay already present in the
    current context; nested registrations therefore behave as a stack. The
    token-restoration discipline is documented on
    :class:`_TokenRegistration`.

    The overlay itself is overlay-only. For effective modes other than
    :class:`~cuprum.context.EnvMode.REPLACE`, the live :func:`os.environ` is
    read at subprocess spawn time, when
    :func:`~cuprum.context.env_overlay.render_env` renders the composed policy,
    so any updates to the process environment after the registration is created
    — for example via ``pytest``'s ``monkeypatch.setenv`` — remain visible to
    subprocesses spawned inside the scope. This is the behaviour the issue
    requires. A ``REPLACE`` mode is the exception: it renders from an empty
    environment, so the live process environment stays out of the child
    entirely.
    """

    __slots__ = ("_overlay",)

    def __init__(self, overlay: EnvOverlay, mode: EnvMode = EnvMode.OVERLAY) -> None:
        """Register an environment overlay in the current context.

        Parameters
        ----------
        overlay:
            Environment values to apply in the enclosing scope.
        mode:
            Policy for combining ``overlay`` with the current context.
            ``EnvMode.OVERLAY`` (the default) layers it onto the overlay
            already in scope; ``EnvMode.REPLACE`` discards that outer overlay
            and renders from ``overlay`` alone.
        """
        super().__init__()
        self._overlay = _coerce_env_overlay(overlay)
        self._install(current_context().with_env_overlay(self._overlay, mode))

    @property
    def overlay(self) -> EnvOverlay | None:
        """The immutable overlay this registration applied."""
        return self._overlay


def env(
    *overlays: cabc.Mapping[str, EnvOverlayValue],
    **kwvars: EnvOverlayValue | EnvMode,
) -> EnvRegistration:
    """Overlay environment variables on top of the live :func:`os.environ`.

    Mirrors :func:`dict` in how arguments are combined: positional mappings
    are merged left-to-right and any keyword arguments win over them. For
    effective modes other than :class:`~cuprum.context.EnvMode.REPLACE`, values
    are not snapshot against ``os.environ`` — the live process environment is
    read at subprocess spawn time so that variables set after Cuprum is
    imported (for example by ``monkeypatch.setenv``) remain visible. A
    ``REPLACE`` mode renders from an empty environment instead, so the live
    process environment stays out of the child.

    Parameters
    ----------
    overlays:
        Zero or more ``Mapping[str, str | UnsetType]`` instances supplying
        overlay entries. Useful when the variable name is not a valid Python
        identifier.
    mode:
        When an :class:`EnvMode`, the policy contributed by this scope.
        ``OVERLAY`` remains the default; another value creates the environment
        variable named ``mode`` for compatibility.
    kwvars:
        Keyword pairs naming environment variables or ``UNSET`` markers.
        Identifier-safe variable names are typically expressed this way.

    Returns
    -------
    EnvRegistration
        A handle that can be detached or used as a context manager.

    Example
    -------
    >>> import os
    >>> os.environ["GIT_AUTHOR_NAME"] = "Cuprum"
    >>> with env(PATH="/usr/bin"):
    ...     # Subprocesses spawned here see PATH=/usr/bin overlaid on the
    ...     # *live* os.environ, including GIT_AUTHOR_NAME.
    ...     pass

    Notes
    -----
    ``env`` is bound to the :class:`~contextvars.Context` in which it is
    created. Detach it in that same logical context (thread or task) to avoid
    ``ValueError`` from :meth:`~contextvars.ContextVar.reset`.
    """
    raw_mode = kwvars.get("mode")
    mode = EnvMode.OVERLAY
    if isinstance(raw_mode, EnvMode):
        mode = raw_mode
        del kwvars["mode"]

    merged: dict[str, EnvOverlayValue] = {}
    for overlay in overlays:
        merged.update(overlay)
    merged.update(typ.cast("cabc.Mapping[str, EnvOverlayValue]", kwvars))
    return EnvRegistration(merged, mode)
