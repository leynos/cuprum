"""Shared Hypothesis strategies for Cuprum unit tests."""

from __future__ import annotations

import typing as typ

from hypothesis import strategies as st

from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from hypothesis.strategies import SearchStrategy


def programs() -> SearchStrategy[Program]:
    """Generate valid program names."""
    return st.from_regex(r"[a-z][a-z0-9_-]{0,15}", fullmatch=True).map(Program)


def allowlists() -> SearchStrategy[frozenset[Program]]:
    """Generate finite allowlists."""
    return st.frozensets(programs(), max_size=10)


def optional_allowlists() -> SearchStrategy[frozenset[Program] | None]:
    """Generate optional allowlists."""
    return st.none() | allowlists()


def timeouts() -> SearchStrategy[float | None]:
    """Generate valid optional timeout values."""
    return st.none() | st.floats(
        min_value=0.0,
        max_value=3600.0,
        allow_nan=False,
        allow_infinity=False,
    )


def hook_tuples() -> SearchStrategy[tuple[cabc.Callable[..., None], ...]]:
    """Generate tuples of identity-distinct no-op hook callables."""

    def make_hook(index: int) -> cabc.Callable[..., None]:
        """Build a distinct no-op hook closing over ``index``."""

        def hook(*args: object) -> None:
            """Discard arguments while capturing ``index`` for identity."""
            # Force closure capture and avoid unused-variable warnings.
            _ = (index, args)

        return hook

    return st.lists(st.integers(), max_size=10, unique=True).map(
        lambda indexes: tuple(make_hook(index) for index in indexes),
    )
