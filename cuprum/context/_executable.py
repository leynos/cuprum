"""Executable-binding policy shared by the context dataclasses.

The `CuprumContext` and `ScopeConfig` dataclasses each carry an
`executable_bindings` layer, coerced to an immutable snapshot on construction.
Deriving the context-facing behaviour here rather than inside
:mod:`cuprum.context.core` keeps that module inside the repository's
400-line-per-module ceiling without trimming the explanations the behaviour
needs (ADR-006 sets the same precedent for the rest of the package).

The mixins hold no ``ContextVar`` state and perform no resolution against the
filesystem; they read and compose value objects only.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.executable_binding import ExecutableResolutionError, resolve_binding

if typ.TYPE_CHECKING:
    from cuprum.context.executable_overlay import ExecutableBindingOverlay
    from cuprum.executable_binding import ExecutableBinding
    from cuprum.program import Program

__all__ = ["_ExecutableBindingPolicy"]


class _ExecutableBindingSource:
    """Field, coercion, and lookup for a context's executable bindings.

    A binding is additive policy for a program the allowlist already permits:
    it supplies the executable to run, not the permission to run it. Nothing
    here consults or amends the allowlist.
    """

    __slots__ = ()

    if typ.TYPE_CHECKING:
        # Supplied by the dataclass that mixes this in. Declared here so the
        # methods below type-check against the field they read.
        executable_bindings: ExecutableBindingOverlay | None

    def executable_binding(self, program: Program) -> ExecutableBinding | None:
        """Return the binding for *program*, or ``None`` when it is unbound.

        Parameters
        ----------
        program : Program
            The logical program whose binding is wanted.

        Returns
        -------
        ExecutableBinding | None
            The effective binding, or ``None`` when the program runs under the
            name it was catalogued with.

        Examples
        --------
        >>> from cuprum.context import CuprumContext
        >>> CuprumContext().executable_binding("echo") is None
        True
        """
        if self.executable_bindings is None:
            return None
        return self.executable_bindings.get(program)


class _ExecutableBindingPolicy(_ExecutableBindingSource):
    """Adds the update and resolution operations used by `CuprumContext`."""

    __slots__ = ()

    if typ.TYPE_CHECKING:
        # ``dc.replace`` needs the concrete class to be a dataclass; the
        # declaration tells a type checker what the runtime already knows.
        __dataclass_fields__: typ.ClassVar[dict[str, dc.Field[typ.Any]]]

    def with_executable_binding(
        self,
        program: Program,
        binding: ExecutableBinding,
    ) -> typ.Self:
        """Return a context whose binding for *program* is *binding*.

        Rebinding a program replaces the executable for the derived context
        and its descendants; the receiver is unchanged.

        Parameters
        ----------
        program : Program
            The logical program to bind.
        binding : ExecutableBinding
            The executable the program should run.

        Returns
        -------
        CuprumContext
            A new context carrying the binding.

        Examples
        --------
        >>> from cuprum.context import CuprumContext
        >>> from cuprum.executable_binding import executable_binding
        >>> ctx = CuprumContext().with_executable_binding(
        ...     "echo", executable_binding("echo", "/bin/echo")
        ... )
        >>> ctx.resolve_executable("echo", cwd=None)
        '/bin/echo'
        """
        current = dict(self.executable_bindings or {})
        current[program] = binding
        return dc.replace(self, executable_bindings=current)

    def resolve_executable(self, program: Program, *, cwd: str | None) -> str | None:
        """Return the executable to run for *program*, or ``None``.

        Resolution is deliberately independent of allowlist enforcement: the
        allowlist decides whether a program may run, and this method decides
        only what a permitted program runs. Keeping the two apart means an
        unpermitted program cannot become runnable by acquiring a binding, and
        a refusal still names the logical program rather than a path.

        Parameters
        ----------
        program : Program
            The logical program to resolve.
        cwd : str | None
            The working directory the execution will run in. A relative bound
            path is anchored at it; ``None`` leaves the platform to resolve
            the path's own spelling.

        Returns
        -------
        str | None
            The executable string, or ``None`` when the program is unbound and
            should run under its catalogued name.

        Raises
        ------
        ExecutableResolutionError
            A bound program's resolver failed. The error names *program* and
            chains the resolver's own exception as ``__cause__``.

        Examples
        --------
        >>> from cuprum.context import CuprumContext
        >>> CuprumContext().resolve_executable("echo", cwd=None) is None
        True
        """
        binding = self.executable_binding(program)
        if binding is None:
            return None
        try:
            return resolve_binding(binding, cwd=cwd)
        except ExecutableResolutionError:
            raise
        except Exception as exc:
            # A resolver is caller-supplied and may fail for any reason at all:
            # a missing virtual environment, an uninstalled toolchain, a bug in
            # the resolver itself. None of those is meaningful to a caller as
            # whichever exception surfaced, and an escaping ``FileNotFoundError``
            # in particular names a path the caller never configured while
            # saying nothing about the binding. This is the one place that knows
            # both the program and the failure, so it is where they are joined.
            raise ExecutableResolutionError(program, exc) from exc
