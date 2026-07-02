"""Execution context with scoped allowlists and hooks.

CuprumContext provides a ContextVar-backed execution context that scopes
allowlists and hooks for command execution. Contexts support narrowing
(restricting the allowlist) and hook registration with deterministic ordering.

Example:
>>> from cuprum.context import ScopeConfig, scoped, before, current_context
>>> from cuprum.catalogue import ECHO
>>> def log_hook(cmd):
...     print(f"Running: {cmd}")
>>> with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
...     with before(log_hook):
...         ctx = current_context()
...         ctx.is_allowed(ECHO)
True

Scoped environment overlays (issue #100): four public symbols layer
environment variables on top of the live ``os.environ`` for subprocesses
spawned inside the scope. ``env(*overlays, **kwvars)`` is a factory that
returns an :class:`EnvRegistration` applying an immutable overlay to the
active context, resolved live at subprocess spawn time against
``os.environ``. :class:`EnvRegistration` is the handle returned by
``env()``; it works as a context manager and exposes :meth:`detach` for
explicit teardown. ``merge_env_overlays(parent, child)`` is the
overlay-only merge that returns an immutable :class:`MappingProxyType`
without reading ``os.environ`` — it is the helper used to record the
effective overlay in observation events. ``resolve_env(*layers)`` is the
spawn-time merge of ``os.environ`` with one or more overlay layers; it
returns a plain ``dict`` or ``None`` when no layers contribute. Precedence
at spawn time, from lowest to highest, is: live ``os.environ`` < scoped
``env()`` overlays (innermost wins) < per-call ``ExecutionContext.env``.
This deliberately diverges from plumbum's ``local.env``, which snapshots
``os.environ`` at module import time and therefore misses variables added
to the process environment after import. The :class:`CuprumContext` and
:class:`ScopeConfig` dataclasses each carry an ``env_overlay`` field
(``Mapping[str, str] | None``) that is coerced to an immutable proxy on
construction.

>>> from cuprum.context import env, current_context
>>> with env(MY_VAR="hello"):
...     current_context().env_overlay["MY_VAR"]
'hello'

"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import os
import typing as typ
from contextvars import ContextVar, Token
from types import MappingProxyType

if typ.TYPE_CHECKING:
    from cuprum.events import ExecHook
    from cuprum.program import Program
    from cuprum.sh import CommandResult, SafeCmd


type BeforeHook = cabc.Callable[[SafeCmd], None]
type AfterHook = cabc.Callable[[SafeCmd, CommandResult], None]


def _coerce_env_overlay(
    overlay: cabc.Mapping[str, str] | None,
) -> cabc.Mapping[str, str] | None:
    """Return an immutable snapshot of an env overlay, or ``None``.

    Overlays are stored as :class:`types.MappingProxyType` views of a frozen
    ``dict`` so that ``CuprumContext`` stays effectively immutable. The overlay
    itself is intentionally *not* materialised against :func:`os.environ`; the
    live process environment is read at subprocess spawn time, after which the
    overlay is layered on top.
    """
    if overlay is None:
        return None
    return MappingProxyType(dict(overlay))


def merge_env_overlays(
    parent: cabc.Mapping[str, str] | None,
    child: cabc.Mapping[str, str] | None,
) -> cabc.Mapping[str, str] | None:
    """Layer ``child`` over ``parent``; ``None`` means *inherit unchanged*.

    Both layers are kept overlay-only — they never include a snapshot of
    :func:`os.environ`. The live process environment is read at spawn time so
    that callers can monkey-patch or otherwise mutate ``os.environ`` after
    Cuprum has been imported and still have those updates visible to
    subprocesses spawned inside the scope.

    Exposed as public API so that other cuprum modules — and downstream
    code that builds custom observation tags — can merge overlay layers
    without reaching for a private symbol.
    """
    if parent is None and child is None:
        return None
    if parent is None:
        return _coerce_env_overlay(child)
    if child is None:
        # Even if ``parent`` is already a MappingProxyType from a prior
        # coerce, callers may pass a plain dict; return an immutable
        # snapshot so the result never aliases a caller-mutable object.
        return _coerce_env_overlay(parent)
    merged = dict(parent)
    merged.update(child)
    return MappingProxyType(merged)


def resolve_env(
    *layers: cabc.Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Merge ``os.environ`` (read live) with the supplied overlay layers.

    Layers are applied left-to-right; later values win. ``None`` *and empty*
    layers are skipped — an empty overlay contributes nothing, so treating it
    as a no-op avoids an unnecessary copy of :func:`os.environ` and lets the
    subprocess inherit the parent environment directly. When every layer is
    skipped the function returns ``None`` so callers may pass it through to
    ``subprocess`` APIs to mean *inherit the parent environment unchanged*.

    The call to :func:`os.environ.copy` is deferred until at least one
    overlay is non-empty, so the result reflects whatever the process
    environment looks like at the moment of resolution — the exact behaviour
    the issue requires.
    """
    if all(not layer for layer in layers):
        return None
    merged: dict[str, str] = os.environ.copy()
    for layer in layers:
        if not layer:
            continue
        merged.update(layer)
    return merged


class ForbiddenProgramError(PermissionError):
    """Raised when attempting to run a program not in the current allowlist."""


def _validate_timeout(timeout: float | None, class_name: str) -> float | None:
    """Validate and coerce timeout value.

    Parameters
    ----------
    timeout:
        The timeout value to validate. May be None, float, or int.
    class_name:
        Name of the class for error messages.

    Returns
    -------
    float | None
        The validated timeout as a float, or None.

    Raises
    ------
    ValueError
        When timeout is negative.

    """
    if timeout is None:
        return None
    timeout_float = float(timeout)
    if timeout_float < 0:
        msg = f"{class_name} timeout must be non-negative, got {timeout_float}"
        raise ValueError(msg)
    return timeout_float


@dc.dataclass(frozen=True, slots=True)
class ScopeConfig:
    """Configuration object for scoped execution context updates.

    Attributes
    ----------
    allowlist:
        Optional allowlist for the scope. When ``None``, inherit the current
        allowlist.
    before_hooks:
        Hooks invoked before command execution (FIFO order).
    after_hooks:
        Hooks invoked after command execution (LIFO order).
    observe_hooks:
        Hooks invoked for structured execution events.
    timeout:
        Optional default timeout in seconds for calls within the scope.
    env_overlay:
        Optional immutable environment overlay layered over the live
        ``os.environ`` at subprocess spawn time. When ``None``, no overlay
        is applied within the scope.

    """

    allowlist: frozenset[Program] | None = None
    before_hooks: tuple[BeforeHook, ...] = ()
    after_hooks: tuple[AfterHook, ...] = ()
    observe_hooks: tuple[ExecHook, ...] = ()
    timeout: float | None = None
    env_overlay: cabc.Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """Validate and coerce timeout after initialization."""
        validated = _validate_timeout(self.timeout, "ScopeConfig")
        # Use object.__setattr__ because the dataclass is frozen
        object.__setattr__(self, "timeout", validated)
        object.__setattr__(
            self,
            "env_overlay",
            _coerce_env_overlay(self.env_overlay),
        )


@dc.dataclass(frozen=True, slots=True)
class CuprumContext:
    """Immutable execution context holding allowlist and hooks.

    Attributes
    ----------
    allowlist:
        Frozenset of programs permitted in this context.
    before_hooks:
        Tuple of hooks invoked before command execution (FIFO order).
    after_hooks:
        Tuple of hooks invoked after command execution (LIFO order).
    observe_hooks:
        Tuple of hooks invoked for structured execution events (FIFO order).
    timeout:
        Optional default timeout in seconds applied when a call does not supply
        an explicit timeout.
    env_overlay:
        Optional immutable environment overlay layered over the live
        ``os.environ`` when command environments are resolved. When ``None``,
        no overlay is active on this context.

    """

    allowlist: frozenset[Program] = dc.field(default_factory=frozenset)
    before_hooks: tuple[BeforeHook, ...] = ()
    after_hooks: tuple[AfterHook, ...] = ()
    observe_hooks: tuple[ExecHook, ...] = ()
    timeout: float | None = None
    env_overlay: cabc.Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """Validate and coerce timeout after initialization."""
        validated = _validate_timeout(self.timeout, "CuprumContext")
        # Use object.__setattr__ because the dataclass is frozen
        object.__setattr__(self, "timeout", validated)
        object.__setattr__(
            self,
            "env_overlay",
            _coerce_env_overlay(self.env_overlay),
        )

    def is_allowed(self, program: Program) -> bool:
        """Return True when the program is in the allowlist.

        Note: An empty allowlist returns False for all programs, but
        check_allowed() treats an empty allowlist as permissive.
        Use check_allowed() for enforcement with permissive defaults.
        """
        return program in self.allowlist

    def check_allowed(self, program: Program) -> None:
        """Raise ForbiddenProgramError if program is not allowed.

        When the allowlist is empty, all programs are permitted (permissive
        default). This allows gradual adoption: code can run without explicit
        context setup, and scoped(ScopeConfig()) can later establish restrictions.
        """
        if not self.allowlist:
            return  # Empty allowlist permits all programs
        if not self.is_allowed(program):
            msg = f"Program '{program}' is not allowed in the current context"
            raise ForbiddenProgramError(msg)

    def narrow(self, config: ScopeConfig) -> CuprumContext:
        """Create a derived context with narrowed allowlist and extended hooks.

        Parameters
        ----------
        config:
            Scope configuration describing allowlist and hook updates.

        Returns
        -------
        CuprumContext
            A new context with narrowed permissions and extended hooks.

        Notes
        -----
        When the parent has an empty allowlist, the provided allowlist is used
        directly to establish a base scope. When the parent has programs, the
        new allowlist is intersected to enforce narrowing (can only remove, not
        add programs). This ensures safety while allowing initial setup.

        """
        if config.allowlist is None:
            new_allowlist = self.allowlist
        elif self.allowlist:
            # Parent has programs: intersect to narrow
            new_allowlist = self.allowlist & config.allowlist
        else:
            # Parent is empty: use provided allowlist as new base
            new_allowlist = config.allowlist

        new_before = self.before_hooks + config.before_hooks
        # After hooks run inner-to-outer, so prepend new hooks
        new_after = config.after_hooks + self.after_hooks
        new_observe = self.observe_hooks + config.observe_hooks
        new_timeout = self.timeout if config.timeout is None else config.timeout
        new_env_overlay = merge_env_overlays(self.env_overlay, config.env_overlay)

        return CuprumContext(
            allowlist=new_allowlist,
            before_hooks=new_before,
            after_hooks=new_after,
            observe_hooks=new_observe,
            timeout=new_timeout,
            env_overlay=new_env_overlay,
        )

    def with_allowlist(self, allowlist: frozenset[Program]) -> CuprumContext:
        """Return a context with the given allowlist replacing the current one.

        Unlike narrow(), this sets the allowlist directly without intersection.
        Use with care; prefer narrow() for enforcing safety invariants.
        """
        return dc.replace(self, allowlist=allowlist)

    def with_before_hook(self, hook: BeforeHook) -> CuprumContext:
        """Return a context with an additional before hook."""
        return dc.replace(self, before_hooks=(*self.before_hooks, hook))

    def without_before_hook(self, hook: BeforeHook) -> CuprumContext:
        """Return a context with the specified before hook removed."""
        new_hooks = tuple(h for h in self.before_hooks if h is not hook)
        return dc.replace(self, before_hooks=new_hooks)

    def with_after_hook(self, hook: AfterHook) -> CuprumContext:
        """Return a context with an additional after hook (prepended for LIFO)."""
        return dc.replace(self, after_hooks=(hook, *self.after_hooks))

    def without_after_hook(self, hook: AfterHook) -> CuprumContext:
        """Return a context with the specified after hook removed."""
        new_hooks = tuple(h for h in self.after_hooks if h is not hook)
        return dc.replace(self, after_hooks=new_hooks)

    def with_observe_hook(self, hook: ExecHook) -> CuprumContext:
        """Return a context with an additional observe hook."""
        return dc.replace(self, observe_hooks=(*self.observe_hooks, hook))

    def without_observe_hook(self, hook: ExecHook) -> CuprumContext:
        """Return a context with the specified observe hook removed."""
        new_hooks = tuple(h for h in self.observe_hooks if h is not hook)
        return dc.replace(self, observe_hooks=new_hooks)

    def with_program(self, program: Program) -> CuprumContext:
        """Return a context with the program added to the allowlist."""
        return dc.replace(self, allowlist=self.allowlist | {program})

    def without_program(self, program: Program) -> CuprumContext:
        """Return a context with the program removed from the allowlist."""
        return dc.replace(self, allowlist=self.allowlist - {program})

    def with_env_overlay(
        self,
        overlay: cabc.Mapping[str, str] | None,
    ) -> CuprumContext:
        """Return a context whose env overlay is layered with ``overlay``.

        Values in ``overlay`` win over earlier overlay entries, but no value
        ever displaces the live :func:`os.environ`; the live process
        environment is read at subprocess spawn time. Passing ``None`` returns
        an unchanged copy.
        """
        return dc.replace(
            self,
            env_overlay=merge_env_overlays(self.env_overlay, overlay),
        )


# Global ContextVar for the current execution context.
# Default is the singleton _DEFAULT_CONTEXT which is immutable (frozen dataclass).
_DEFAULT_CONTEXT = CuprumContext()
_current_context: ContextVar[CuprumContext] = ContextVar(
    "cuprum_context",
    default=_DEFAULT_CONTEXT,
)


def current_context() -> CuprumContext:
    """Return the current execution context."""
    return _current_context.get()


def get_context() -> CuprumContext:
    """Alias for current_context()."""
    return current_context()


def _set_context(ctx: CuprumContext) -> Token[CuprumContext]:
    """Set the current context and return a token for restoration."""
    return _current_context.set(ctx)


def _reset_context(token: Token[CuprumContext]) -> None:
    """Restore the context to its previous state using the token."""
    _current_context.reset(token)


class _ScopedContext:
    """Context manager for entering a scoped execution context."""

    __slots__ = ("_ctx", "_token")

    def __init__(self, config: ScopeConfig) -> None:
        """Narrow the current context with ``config`` for later entry."""
        parent = current_context()
        self._ctx = parent.narrow(config)
        self._token: Token[CuprumContext] | None = None

    def __enter__(self) -> CuprumContext:
        """Activate the scoped context and return it."""
        self._token = _set_context(self._ctx)
        return self._ctx

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Restore the previous context on scope exit."""
        if self._token is not None:
            _reset_context(self._token)


def scoped(config: ScopeConfig) -> _ScopedContext:
    """Create a scoped context manager for narrowed execution.

    Parameters
    ----------
    config:
        Scope configuration describing allowlist and hook updates.

    Returns
    -------
    _ScopedContext
        A context manager that narrows the current context.

    Example
    -------
    >>> with scoped(ScopeConfig(allowlist=frozenset([ECHO]))) as ctx:
    ...     assert ctx.is_allowed(ECHO)

    """
    return _ScopedContext(config)


class AllowRegistration:
    """Registration handle for dynamic allowlist extension.

    Supports detach() and context manager usage for scoped allowing.

    Token-based Restoration
    -----------------------
    The registration captures a token at creation time. When detach() is called,
    the original context is restored via the token, ensuring no context pollution
    even when used outside scoped(ScopeConfig()) blocks. This means detach()
    restores the exact context that existed when the registration was created,
    regardless of subsequent context modifications. If multiple registrations are
    created and detached in non-LIFO order, earlier tokens may restore states
    that remove programs added by other registrations.

    Detach in the same logical Context (thread or Task) in which the
    registration was created. Resetting a ContextVar with a token from a
    different Context raises ValueError and would break this guarantee.
    """

    __slots__ = ("_detached", "_programs", "_token")

    def __init__(self, *programs: Program) -> None:
        """Create an allowlist registration and add programs to current context."""
        self._programs = frozenset(programs)
        self._detached = False
        # Add programs to current context and capture token for restoration
        ctx = current_context()
        new_ctx = dc.replace(ctx, allowlist=ctx.allowlist | self._programs)
        self._token: Token[CuprumContext] | None = _set_context(new_ctx)

    def detach(self) -> None:
        """Restore the original context via the captured token."""
        if self._detached:
            return
        self._detached = True
        if self._token is not None:
            _reset_context(self._token)
            self._token = None

    def __enter__(self) -> AllowRegistration:
        """Enter context manager; programs are already registered."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context manager; detach registered programs."""
        self.detach()


def allow(*programs: Program) -> AllowRegistration:
    """Extend the current context's allowlist with additional programs.

    Parameters
    ----------
    programs:
        Programs to add to the allowlist.

    Returns
    -------
    AllowRegistration
        A handle that can be detached or used as a context manager.

    Example
    -------
    >>> with allow(LS):
    ...     assert current_context().is_allowed(LS)

    """
    return AllowRegistration(*programs)


class HookRegistration:
    """Registration handle for hooks with detach() and context manager support.

    Token-based Restoration
    -----------------------
    The registration captures a token at creation time. When detach() is called,
    the original context is restored via the token, ensuring no context pollution
    even when used outside scoped(ScopeConfig()) blocks. This means detach()
    restores the exact context that existed when the registration was created,
    regardless of subsequent context modifications.

    As with AllowRegistration, detach the hook only from the Context where
    it was registered; using the token in a different Context will raise
    ValueError in the standard library.
    """

    __slots__ = ("_detached", "_hook", "_hook_type", "_token")

    def __init__(
        self,
        hook: BeforeHook | AfterHook | ExecHook,
        hook_type: typ.Literal["before", "after", "observe"],
    ) -> None:
        """Create a hook registration and add hook to current context."""
        self._hook = hook
        self._hook_type = hook_type
        self._detached = False
        # Add hook to current context and capture token for restoration
        ctx = current_context()
        if hook_type == "before":
            new_ctx = ctx.with_before_hook(typ.cast("BeforeHook", hook))
        elif hook_type == "after":
            new_ctx = ctx.with_after_hook(typ.cast("AfterHook", hook))
        else:
            new_ctx = ctx.with_observe_hook(typ.cast("ExecHook", hook))
        self._token: Token[CuprumContext] | None = _set_context(new_ctx)

    def detach(self) -> None:
        """Restore the original context via the captured token."""
        if self._detached:
            return
        self._detached = True
        if self._token is not None:
            _reset_context(self._token)
            self._token = None

    def __enter__(self) -> HookRegistration:
        """Enter context manager; hook is already registered."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context manager; detach registered hook."""
        self.detach()


def before(hook: BeforeHook) -> HookRegistration:
    """Register a before-execution hook in the current context.

    Parameters
    ----------
    hook:
        Callable invoked with the SafeCmd before execution.

    Returns
    -------
    HookRegistration
        A handle that can be detached or used as a context manager.

    Example
    -------
    >>> def log_cmd(cmd):
    ...     print(f"Running: {cmd.program}")
    >>> with before(log_cmd):
    ...     # Commands run here will trigger log_cmd
    ...     pass

    """
    return HookRegistration(hook, "before")


def after(hook: AfterHook) -> HookRegistration:
    """Register an after-execution hook in the current context.

    Parameters
    ----------
    hook:
        Callable invoked with the SafeCmd and CommandResult after execution.

    Returns
    -------
    HookRegistration
        A handle that can be detached or used as a context manager.

    Example
    -------
    >>> def log_result(cmd, result):
    ...     print(f"Finished: {cmd.program} -> {result.exit_code}")
    >>> with after(log_result):
    ...     # Commands run here will trigger log_result
    ...     pass

    """
    return HookRegistration(hook, "after")


class EnvRegistration:
    """Registration handle for a scoped environment overlay.

    Like :class:`AllowRegistration` and :class:`HookRegistration`, the handle
    captures a :class:`~contextvars.Token` at creation time and uses it to
    restore the previous context on :meth:`detach`. The overlay is layered on
    top of any overlay already present in the current context; nested
    registrations therefore behave as a stack.

    The overlay itself is overlay-only. The live :func:`os.environ` is read at
    subprocess spawn time (see :func:`resolve_env`), so any updates to the
    process environment after the registration is created — for example via
    ``pytest``'s ``monkeypatch.setenv`` — remain visible to subprocesses
    spawned inside the scope. This is the behaviour the issue requires.

    Token-based Restoration
    -----------------------
    Identical to :class:`AllowRegistration` and :class:`HookRegistration`,
    :meth:`detach` resets the captured :class:`~contextvars.Token` and
    therefore restores the exact context that existed when the registration
    was created. Detach in LIFO order: detaching an outer registration before
    an inner one resets the underlying ``ContextVar`` to the outer's snapshot
    and silently discards the inner overlay along with any other context
    updates layered after registration. Consumers that need finer control
    should prefer ``with`` blocks, which already detach in LIFO order.

    Detach in the same logical :class:`~contextvars.Context` (thread or
    task) in which the registration was created. Resetting a
    :class:`~contextvars.ContextVar` with a token from a different context
    raises :class:`ValueError`.
    """

    __slots__ = ("_detached", "_overlay", "_token")

    def __init__(self, overlay: cabc.Mapping[str, str]) -> None:
        """Layer ``overlay`` onto the current context's env overlay."""
        self._overlay = _coerce_env_overlay(overlay)
        self._detached = False
        ctx = current_context()
        new_ctx = ctx.with_env_overlay(self._overlay)
        self._token: Token[CuprumContext] | None = _set_context(new_ctx)

    @property
    def overlay(self) -> cabc.Mapping[str, str] | None:
        """Return the immutable overlay this registration applied."""
        return self._overlay

    def detach(self) -> None:
        """Restore the original context via the captured token."""
        if self._detached:
            return
        self._detached = True
        if self._token is not None:
            _reset_context(self._token)
            self._token = None

    def __enter__(self) -> EnvRegistration:
        """Enter context manager; overlay is already applied."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context manager; detach the overlay."""
        self.detach()


def env(
    *overlays: cabc.Mapping[str, str],
    **kwvars: str,
) -> EnvRegistration:
    """Overlay environment variables on top of the live :func:`os.environ`.

    Mirrors :func:`dict` in how arguments are combined: positional mappings
    are merged left-to-right and any keyword arguments win over them. Values
    are not snapshot against ``os.environ`` — the live process environment is
    read at subprocess spawn time so that variables set after Cuprum is
    imported (for example by ``monkeypatch.setenv``) remain visible.

    Parameters
    ----------
    overlays:
        Zero or more ``Mapping[str, str]`` instances supplying overlay
        entries. Useful when the variable name is not a valid Python
        identifier.
    kwvars:
        Keyword pairs naming environment variables. Identifier-safe variable
        names are typically expressed this way.

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
    Identical to other registration helpers, ``env`` is bound to the
    :class:`~contextvars.Context` in which it is created; detach in the same
    logical context (thread or task) to avoid ``ValueError`` from
    :meth:`~contextvars.ContextVar.reset`.
    """
    merged: dict[str, str] = {}
    for overlay in overlays:
        merged.update(overlay)
    if kwvars:
        merged.update(kwvars)
    return EnvRegistration(merged)


def observe(hook: ExecHook) -> HookRegistration:
    """Register a structured execution event hook in the current context.

    Parameters
    ----------
    hook:
        Callable invoked with :class:`~cuprum.events.ExecEvent` values as Cuprum
        executes commands and pipelines.

    Returns
    -------
    HookRegistration
        A handle that can be detached or used as a context manager.

    """
    return HookRegistration(hook, "observe")


__all__ = [
    "AfterHook",
    "AllowRegistration",
    "BeforeHook",
    "CuprumContext",
    "EnvRegistration",
    "ForbiddenProgramError",
    "HookRegistration",
    "ScopeConfig",
    "after",
    "allow",
    "before",
    "current_context",
    "env",
    "get_context",
    "merge_env_overlays",
    "observe",
    "resolve_env",
    "scoped",
]
