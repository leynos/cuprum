"""Typed bindings from a logical program identity to an executable path.

A :class:`~cuprum.program.Program` names an executable for catalogue and policy
purposes, and the same value is handed to the operating system as ``argv[0]``.
Those two jobs usually coincide, but not always: a tool may live at a
version-pinned absolute path, inside a virtual environment, or be replaced by a
controlled stub during tests. This module carries the second job as its own
value so the first can stay a bounded logical label.

The path vocabulary this module builds on — :data:`ExecutablePath`,
:class:`~cuprum.executable_paths.PathBindingRejection`,
:func:`~cuprum.executable_paths.executable_path`, and
:func:`~cuprum.executable_paths.advisory_path_rejection` — lives in
:mod:`cuprum.executable_paths` and is re-exported here, so callers reach the
whole feature through one import.

Both modules are pure: neither reads or writes
:class:`~cuprum.context.CuprumContext`, and neither imports the context package
at runtime. That keeps the dependency direction acyclic, so the context package
and the execution layer can both depend on them.

Resolution and its limits
-------------------------

:func:`resolve_binding` turns a binding plus a working directory into the string
to execute. It is pure and total for every binding the constructors accept.

Checking a path and then executing it is not atomic. Between the advisory check
and the child's ``exec``, another process with write access to the directory can
replace, rename, or re-link the file, and the child will run whatever is there
at that moment. Cuprum cannot close that window from inside the process: it
would need an ``O_PATH`` descriptor plus ``fexecve``, which is not available
portably or from ``asyncio``. Operators who need the guarantee that the checked
path is the executed binary must supply it from the filesystem, by owning the
directory, restricting write permission to a trusted account, and deploying the
tree read-only.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from pathlib import Path

from cuprum.executable_paths import (
    ExecutablePath,
    InvalidExecutableBindingError,
    PathBindingRejection,
    advisory_path_rejection,
    classify_executable_path,
    coerce_path_string,
    executable_path,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.program import Program

type ExecutableResolver = cabc.Callable[[], str]
"""A zero-argument callable producing the executable to run.

Evaluated once per execution, at spawn time. It exists for callers whose
executable is only knowable late: a virtual environment concretised during the
run, a toolchain selected from configuration, or a test double chosen by the
test's own fixtures. Its result is used verbatim, so a resolver that wants its
result anchored must return an absolute path itself.
"""


@dc.dataclass(frozen=True, slots=True)
class ExecutableBinding:
    """The executable a logical program should run.

    Exactly one of ``path`` and ``resolver`` must be set. A ``path`` binding is
    a fixed, already-validated string; a ``resolver`` binding is evaluated once
    per execution, so it can consult state that is only available at spawn
    time.

    Construct through :func:`executable_binding`, which applies validation and
    reports the logical program in its errors; constructing directly is
    supported for callers that already hold a validated :data:`ExecutablePath`
    or a trusted resolver.

    Parameters
    ----------
    path : ExecutablePath | None, optional
        The fixed executable path.
    resolver : ExecutableResolver | None, optional
        The callable producing the executable at spawn time.

    Raises
    ------
    ValueError
        Neither or both of ``path`` and ``resolver`` were supplied.
    """

    path: ExecutablePath | None = None
    resolver: ExecutableResolver | None = None

    def __post_init__(self) -> None:
        """Reject a binding that does not name exactly one source."""
        if (self.path is None) == (self.resolver is None):
            supplied = "neither" if self.path is None else "both"
            msg = (
                "ExecutableBinding requires exactly one of 'path' or 'resolver'; "
                f"got {supplied}"
            )
            raise ValueError(msg)

    @property
    def is_lazy(self) -> bool:
        """Whether the executable is computed at spawn time."""
        return self.resolver is not None


def executable_binding(
    program: Program,
    path_or_resolver: str | Path | ExecutableResolver,
    *,
    allow_relative: bool = False,
) -> ExecutableBinding:
    """Bind a logical *program* to an executable path or resolver.

    A callable is stored as a resolver and used verbatim; a string or path is
    validated through :func:`~cuprum.executable_paths.executable_path` and
    stored as a normalized path.

    Parameters
    ----------
    program : Program
        The logical identity the binding is for. It is used only for error
        reporting; the binding does not itself restrict which program may use
        it.
    path_or_resolver : str | Path | ExecutableResolver
        The executable path, or a zero-argument callable returning one.
    allow_relative : bool, optional
        When True, a relative path is permitted. Ignored for a resolver, whose
        result Cuprum does not inspect. Defaults to False.

    Returns
    -------
    ExecutableBinding
        The binding.

    Raises
    ------
    InvalidExecutableBindingError
        A supplied path failed validation. The error names the program, quotes
        the path, and carries the classified reason.

    Examples
    --------
    >>> binding = executable_binding("tool", "/opt/tools/tool")
    >>> binding.path
    '/opt/tools/tool'
    >>> binding.is_lazy
    False
    """
    # Discriminate on the path types rather than on ``callable``: the negative
    # branch of a callable test leaves the union un-narrowed, and the intended
    # reading is "a resolver is anything that is not a path".
    if not isinstance(path_or_resolver, (str, Path)):
        return ExecutableBinding(resolver=path_or_resolver)
    raw_value = coerce_path_string(path_or_resolver)
    rejection = classify_executable_path(raw_value, allow_relative=allow_relative)
    if rejection is not None:
        raise InvalidExecutableBindingError(program, raw_value, rejection)
    return ExecutableBinding(
        path=executable_path(raw_value, allow_relative=allow_relative)
    )


def resolve_binding(binding: ExecutableBinding, *, cwd: str | None) -> str:
    """Evaluate *binding* into the executable string to run.

    A resolver is called once and its result is used verbatim. A path binding is
    returned as-is when it is already absolute; a relative path binding is
    anchored at ``cwd`` when one is supplied, and returned unchanged otherwise,
    leaving the platform to resolve it — against ``PATH`` for a bare name, or
    relative to the process's own working directory otherwise.

    Parameters
    ----------
    binding : ExecutableBinding
        The binding to evaluate.
    cwd : str | None
        The working directory the execution will run in, or ``None``.

    Returns
    -------
    str
        The executable string.

    Examples
    --------
    >>> resolve_binding(
    ...     executable_binding("tool", "bin/tool", allow_relative=True),
    ...     cwd="/srv/project",
    ... )
    '/srv/project/bin/tool'
    """
    if binding.resolver is not None:
        return binding.resolver()
    resolved = Path(str(binding.path))
    if cwd is None or resolved.is_absolute():
        return str(resolved)
    return str(Path(cwd) / resolved)


__all__ = [
    "ExecutableBinding",
    "ExecutablePath",
    "ExecutableResolver",
    "InvalidExecutableBindingError",
    "PathBindingRejection",
    "advisory_path_rejection",
    "classify_executable_path",
    "executable_binding",
    "executable_path",
    "resolve_binding",
]
