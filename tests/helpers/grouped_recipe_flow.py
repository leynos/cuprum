"""Prove grouped pytest targets reach the command that collects them."""

from __future__ import annotations

from tests.helpers.ci_documents import require
from tests.helpers.recipe_read import command_program, command_segments, recipe_tokens

SET_ARGUMENTS_START = 2


def require_grouped_selector_drives_pytest(
    recipe: str,
    *,
    grouped_targets: str,
    selected_targets: str,
    pytest_variable: str,
) -> None:
    """Require the grouped target intersection to reach pytest positionals.

    The grouped branch binds the intersection to shell positionals with
    ``set --``. This checks that the final such binding before a pytest command
    is the exact filter expression and that the command consumes ``$$@``.

    Parameters
    ----------
    recipe : str
        Recipe text, as :func:`tests.helpers.makefile.recipe_of` returns it.
    grouped_targets : str
        Make variable containing the modules assigned to the grouped route.
    selected_targets : str
        Make variable containing the caller-selected modules.
    pytest_variable : str
        Make variable naming the pytest invocation.

    Raises
    ------
    AssertionError
        If no pytest command consumes positional arguments from a preceding
        ``set --`` whose value is the exact grouped and selected intersection.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require.
    commands = command_segments(recipe_tokens(recipe))
    expected_filter = f"$(filter $({grouped_targets}),$({selected_targets}))"
    pytest_program = f"$({pytest_variable})"
    bindings = [
        (position, words)
        for position, words in commands
        if command_program(words) == "set"
        and len(words) > SET_ARGUMENTS_START
        and words[1] == "--"
    ]
    invocations = [
        (position, words)
        for position, words in commands
        if command_program(words) == pytest_program
    ]

    require(
        condition=_consumes_intersection(bindings, invocations, expected_filter),
        message=(
            f"the grouped pytest branch must pass `$$@` from its final `set -- "
            f"{expected_filter}` binding to `{pytest_program}`"
        ),
    )


def _consumes_intersection(
    bindings: list[tuple[int, tuple[str, ...]]],
    invocations: list[tuple[int, tuple[str, ...]]],
    expected_filter: str,
) -> bool:
    """Return whether pytest consumes the last ``set --`` before it."""
    return any(
        " ".join(binding[2:]) == expected_filter
        and "$$@" in invocation
        and _is_final_binding_before(bindings, binding_at, invocation_at)
        for invocation_at, invocation in invocations
        for binding_at, binding in bindings
    )


def _is_final_binding_before(
    bindings: list[tuple[int, tuple[str, ...]]],
    position: int,
    limit: int,
) -> bool:
    """Return whether the binding at ``position`` is the last one before ``limit``."""
    return position < limit and not any(
        position < later < limit for later, _words in bindings
    )
