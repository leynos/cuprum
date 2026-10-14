"""Pin the ``build-wheels`` action's native-wheel cache key and its prefix.

Each native-wheel matrix leg caches ``~/.cache/pip`` and the Cargo registry
under a key assembled from the runner lane, the Rust target triple, the
maturin version, and a hash of ``rust/Cargo.lock``. The key is rendered with
``format()`` rather than interpolation, which splits one component order
across two declarations that can drift apart: the template's ``{n}``
placeholders with their positional argument list, and the ``restore-keys``
prefix a saved key is matched against.

A swapped or omitted component on either side still yields a syntactically
valid key, so the action keeps building wheels and simply never restores. That
surfaces as a slow cache miss rather than a failure, which is why the order is
pinned here rather than left to review.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.workflow_steps import find_step

ACTION = ".github/actions/build-wheels"
STEP = "Cache native wheel build dependencies"

#: The ``format()`` call rendering the key, split into its template and its
#: argument list. Read as text rather than through ``ast`` because
#: ``inputs.maturin-version`` is not a Python attribute path: parsing would
#: rewrite it as a subtraction rather than return it.
_KEY_EXPRESSION = re.compile(
    r"^\$\{\{\s*format\(\s*'(?P<template>[^']*)'\s*(?:,\s*(?P<arguments>.*))?\)\s*\}\}$"
)

#: One ``${{ ... }}`` span, non-greedy to the first ``}}``.
_EXPRESSION_SPAN = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)

#: One positional argument of a ``format()`` call: a quoted literal, a
#: parenthesized call, or a dotted context path. The path tail accepts
#: hyphens so ``inputs.maturin-version`` stays one argument.
_ARGUMENT = re.compile(r"'[^']*'|[\w-]+(?:\([^)]*\))?(?:\.[\w-]+)*")

#: A stand-in value per component source. Each is bracketed so it cannot be
#: mistaken for the literal separators, and none is a substring of another, so
#: a reordered key cannot render to the same string as its original order.
_SAMPLES: typ.Final[dict[str, str]] = {
    "runner.os": "<os>",
    "runner.arch": "<arch>",
    "inputs.target": "<target>",
    "inputs.maturin-version": "<maturin>",
    "hashFiles('rust/Cargo.lock')": "<lock>",
}


def _cache_inputs() -> dict[str, object]:
    """Return the ``with`` block of the action's cache step."""
    step = find_step(action=ACTION, name=STEP)
    return typ.cast("dict[str, object]", step["with"])


def _key_parts() -> tuple[str, list[str]]:
    """Return the key's ``format()`` template and its positional arguments."""
    expression = typ.cast("str", _cache_inputs()["key"])
    match = _KEY_EXPRESSION.match(expression)
    assert match is not None, f"the cache key is not one format() call: {expression!r}"
    return match.group("template"), _ARGUMENT.findall(match.group("arguments") or "")


def _sample(component: str) -> str:
    """Return the stand-in value for one component source."""
    assert component in _SAMPLES, f"no sample value for the component {component!r}"
    return _SAMPLES[component]


def _rendered_key() -> str:
    """Render the cache key from sample components, as the runner would."""
    template, arguments = _key_parts()
    placeholders = template.count("{")
    assert len(arguments) == placeholders, (
        f"{len(arguments)} arguments cannot fill {placeholders} placeholders "
        f"in the template {template!r}"
    )
    return template.format(*(_sample(argument) for argument in arguments))


def _rendered_restore_key() -> str:
    """Render the restore-key prefix from the same sample components."""
    declared = typ.cast("str", _cache_inputs()["restore-keys"]).strip()

    def substitute(span: re.Match[str]) -> str:
        """Replace one ``${{ ... }}`` span with its component's sample value."""
        component = span.group("body").strip()
        assert component in _SAMPLES, (
            f"the restore key reads the component {component!r}, which no "
            f"sample value covers: {declared!r}"
        )
        return _sample(component)

    # Every span must resolve, so the assertion inside `substitute` is reached
    # rather than left unexercised by a prefix that happens to match nothing.
    return _EXPRESSION_SPAN.sub(substitute, declared)


def test_cache_key_renders_every_component_in_a_fixed_order() -> None:
    """The family is scoped by five components, and their order is the key."""
    template, arguments = _key_parts()

    assert template == "cuprum-wheel-{0}-{1}-{2}-{3}-{4}", (
        f"the key template is not the declared one: {template!r}"
    )
    assert arguments == [
        "runner.os",
        "runner.arch",
        "inputs.target",
        "inputs.maturin-version",
        "hashFiles('rust/Cargo.lock')",
    ], f"the key's components are not in the declared order: {arguments!r}"


def test_restore_key_prefix_matches_the_rendered_cache_key() -> None:
    """A saved key must carry the restore key as its prefix, or never restore."""
    assert _rendered_restore_key() == "cuprum-wheel-<os>-<arch>-<target>-<maturin>-", (
        f"the restore key's components are not in the declared order: "
        f"{_rendered_restore_key()!r}"
    )
    assert _rendered_key().startswith(_rendered_restore_key()), (
        f"the restore key {_rendered_restore_key()!r} is not a prefix of the "
        f"rendered key {_rendered_key()!r}, so no saved entry can ever match it"
    )
