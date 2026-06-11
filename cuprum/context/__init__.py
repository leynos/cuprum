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

from cuprum.context.core import (
    AfterHook,
    BeforeHook,
    CuprumContext,
    ForbiddenProgramError,
    ScopeConfig,
)
from cuprum.context.env_overlay import merge_env_overlays, resolve_env
from cuprum.context.registration import (
    AllowRegistration,
    EnvRegistration,
    HookRegistration,
    after,
    allow,
    before,
    env,
    observe,
    scoped,
)
from cuprum.context.state import current_context, get_context
from cuprum.events import ExecHook

__all__ = [
    "AfterHook",
    "AllowRegistration",
    "BeforeHook",
    "CuprumContext",
    "EnvRegistration",
    "ExecHook",
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
