"""Resolve which cache family each save step in the estate publishes.

Every cache key in this repository renders from ``.github/actions/cache-keys``,
so five jobs can hold five save steps that all name ``SCCACHE_CACHE_KEY`` and
still write five disjoint archives: the rendered key carries the runner lane,
the interpreter, and the build shape, none of which appear in the ``env`` name.
Counting writers by ``env`` name therefore either forbids a legitimate split or
permits a genuine collision, depending on which way the manifest is written.

This module resolves the family a save step actually publishes, so
``tests/test_ci_cache_ownership.py`` can hold the real invariant: one writer per
family. Matrix legs are expanded, because one declared step in the interpreter
matrix is four writers of four families at run time, and a save condition that
names a matrix value is honoured, because the leg it excludes writes nothing.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_workflows import job, save_steps, step_inputs, steps

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Step

CACHE_KEYS_ACTION = "./.github/actions/cache-keys"

#: The ``cache-keys`` inputs each key family is scoped by, beyond the runner
#: lane that every family carries. The Cargo registry holds the resolved
#: dependency graph, which no interpreter or build shape changes. The tool
#: archive holds uv's interpreter-specific environments. Compiler output is
#: narrower still: `pyo3` is declared without `abi3`, so an object compiled
#: against one CPython serves no other, and an optimized or instrumented object
#: serves no unoptimized build.
KEY_SCOPES: typ.Final[cabc.Mapping[str, tuple[str, ...]]] = {
    "CARGO_CACHE_KEY": (),
    "TOOL_CACHE_KEY": ("python-version",),
    "SCCACHE_CACHE_KEY": ("python-version", "compiler-shape"),
}

#: ``runner.environment`` renders from the runner label, and the two lanes read
#: different cache services, so an archive can never cross between them.
LANE_OF_LABEL: typ.Final[cabc.Mapping[str, str]] = {
    "ubicloud-standard-2": "self-hosted",
    "ubicloud-standard-4": "self-hosted",
    "ubuntu-latest": "github-hosted",
}

_MATRIX_REFERENCE = re.compile(r"matrix\.([a-z0-9-]+)", re.IGNORECASE)
_EXPRESSION = re.compile(r"^\s*\$\{\{(?P<body>.+)\}\}\s*$", re.DOTALL)


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


class CacheFamily(typ.NamedTuple):
    """One rendered cache family: what a single archive key identifies.

    Attributes
    ----------
    key_name:
        The ``env`` name the key renders from, such as ``SCCACHE_CACHE_KEY``.
    lane:
        ``self-hosted`` or ``github-hosted``, the value ``runner.environment``
        renders to. The two lanes read different cache services.
    scope:
        The ``cache-keys`` inputs this family is scoped by, in the order
        :data:`KEY_SCOPES` declares them.
    """

    key_name: str
    lane: str
    scope: tuple[str, ...]

    def __str__(self) -> str:
        """Return the family as a single readable identifier."""
        return "-".join((self.key_name, self.lane, *self.scope))


def _lane(workflow_name: str, job_name: str) -> str:
    """Return the cache lane a job's runner label resolves to."""
    label = job(workflow_name, job_name).get("runs-on")
    _require(
        condition=label in LANE_OF_LABEL,
        message=f"{workflow_name}:{job_name} runs on unmapped label {label!r}",
    )
    return LANE_OF_LABEL[str(label)]


def matrix_legs(workflow_name: str, job_name: str) -> list[dict[str, object]]:
    """Expand one job's matrix into the legs it runs as.

    Parameters
    ----------
    workflow_name:
        Workflow file name, such as ``ci.yml``.
    job_name:
        Job key within that workflow.

    Returns
    -------
    list[dict[str, object]]
        One mapping per ``include`` entry, or a single empty mapping when the
        job declares no matrix, so callers can iterate uniformly.

    Notes
    -----
    Fails the contract, through :func:`_require`, when the job declares a
    matrix without an ``include`` list, which this reader cannot expand.
    """
    strategy = job(workflow_name, job_name).get("strategy")
    if not isinstance(strategy, dict):
        return [{}]
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return [{}]
    include = matrix.get("include")
    _require(
        condition=isinstance(include, list),
        message=(
            f"{workflow_name}:{job_name} declares a matrix this contract cannot "
            "expand; only `include` lists are supported"
        ),
    )
    return [
        typ.cast("dict[str, object]", leg) for leg in typ.cast("list[object]", include)
    ]


def _resolve(value: object, leg: cabc.Mapping[str, object], message: str) -> str:
    """Render one ``cache-keys`` input against a matrix leg."""
    text = str(value)
    match = _EXPRESSION.match(text)
    if match is None:
        return text
    body = match.group("body").strip()
    reference = _MATRIX_REFERENCE.fullmatch(body)
    _require(
        condition=reference is not None,
        message=f"{message}: cannot resolve {text!r}",
    )
    name = typ.cast("re.Match[str]", reference).group(1)
    _require(
        condition=name in leg,
        message=f"{message}: matrix leg declares no {name!r}",
    )
    return str(leg[name])


def _renderer_inputs(workflow_name: str, job_name: str) -> dict[str, object]:
    """Return the inputs the job passes to the shared key renderer."""
    renderers = [
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == CACHE_KEYS_ACTION
    ]
    _require(
        condition=len(renderers) == 1,
        message=(
            f"{workflow_name}:{job_name} must render its keys through "
            f"{CACHE_KEYS_ACTION} exactly once"
        ),
    )
    return step_inputs(renderers[0], f"{workflow_name}:{job_name} renderer inputs")


def _key_name(step: Step, message: str) -> str:
    """Return the ``env`` name a save step's key expression renders from."""
    key = step_inputs(step, message).get("key")
    match = _EXPRESSION.match(str(key))
    _require(
        condition=match is not None,
        message=f"{message}: key must be an expression, got {key!r}",
    )
    body = typ.cast("re.Match[str]", match).group("body").strip()
    _require(
        condition=body.startswith("env."),
        message=f"{message}: key must render from env, got {key!r}",
    )
    return body.removeprefix("env.")


def _writes_on_leg(step: Step, leg: cabc.Mapping[str, object]) -> bool:
    """Report whether a save step's condition admits one matrix leg.

    Returns
    -------
    bool
        ``True`` when the leg writes, ``False`` when the condition excludes it.

    Notes
    -----
    Only matrix references are evaluated. A condition naming
    ``matrix.python-suite`` excludes the leg that compiles nothing, and that
    exclusion is what keeps the typecheck-only leg from appearing to own a
    family another job writes. Every other clause is a run-time value this
    contract deliberately does not model.
    """
    condition = step.get("if")
    if not isinstance(condition, str):
        return True
    return all(
        bool(leg.get(name, False))
        for name in _MATRIX_REFERENCE.findall(condition)
        if name in leg
    )


def writer_families(workflow_name: str, job_name: str) -> set[CacheFamily]:
    """Resolve every cache family one job publishes.

    Parameters
    ----------
    workflow_name:
        Workflow file name, such as ``ci.yml``.
    job_name:
        Job key within that workflow.

    Returns
    -------
    set[CacheFamily]
        One entry per archive the job's save steps publish, with matrix legs
        expanded and legs excluded by a matrix-valued save condition omitted.

    Notes
    -----
    Fails the contract, through :func:`_require`, when the job renders its keys
    other than exactly once through the shared renderer, when a save step names
    a key with no declared scope, when a scoping input is left to the action's
    default, or when the job publishes one family from more than one save step.
    """
    inputs = _renderer_inputs(workflow_name, job_name)
    lane = _lane(workflow_name, job_name)
    families: set[CacheFamily] = set()
    for step in save_steps(workflow_name, job_name):
        message = f"{workflow_name}:{job_name} save must declare inputs"
        key_name = _key_name(step, message)
        _require(
            condition=key_name in KEY_SCOPES,
            message=f"{message}: {key_name} has no declared scope",
        )
        for leg in matrix_legs(workflow_name, job_name):
            if not _writes_on_leg(step, leg):
                continue
            # Every scoping input must be named explicitly rather than left to
            # the action's default. A default is invisible at the call site,
            # and a family whose scope a reader cannot see in the job is one
            # they cannot check for a second writer.
            missing = [name for name in KEY_SCOPES[key_name] if name not in inputs]
            _require(
                condition=not missing,
                message=f"{message}: must declare {missing}",
            )
            scope = tuple(
                _resolve(inputs[name], leg, message) for name in KEY_SCOPES[key_name]
            )
            family = CacheFamily(key_name, lane, scope)
            # Rejected here rather than deduplicated by the set. Two save steps
            # in one job that render the same family are the same collision the
            # caller's contract exists to catch, and a set would silently make
            # them one element and report the job as a sole writer.
            _require(
                condition=family not in families,
                message=f"{message}: publishes {family} more than once",
            )
            families.add(family)
    return families
