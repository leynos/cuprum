"""Read where a Cuprum CI job can run, and whether it runs at all.

A ``runs-on`` declaration stopped being a label the moment the fork fallback
landed. A pull request from a fork cannot obtain an Ubicloud runner, so every
fork-reachable lane selects its runner from an expression, and a reader that
narrows that value with ``str()`` sees one opaque label that carries no vendor
prefix. Axinite #372 recorded what that costs: the Ubicloud classifier said no,
the job was dropped before any assertion ran, and the lane sat exempt from the
placement, ceiling and registry contracts while still asking for a paid runner.

This module therefore fails towards refusal. It models the three shapes the
repository actually declares and rejects everything else by name, so an
unreadable declaration reddens a contract instead of quietly satisfying one:

``literal``
    A bare label, such as ``windows-2022``.
``fork``
    The canonical fork fallback, read **by position**. Nile-valley #106's
    contract asserted only that the expression named one hosted and one
    Ubicloud label somewhere, so swapping the arms passed while sending forks
    to a runner they cannot obtain.
``matrix``
    ``${{ matrix.<key> }}``, resolved through the ``include`` entries it
    selects. Rstest-bdd #788 showed why the resolution matters: a reader that
    stops at ``matrix.os`` never sees the expression the matrix value holds.

Whether a job *runs* is modelled here too. Lille #349's placement, budget and
cache rules all read a job's declared configuration and none read whether it
executes, so ``if: false`` on a build lane satisfied every one of them while
running nothing.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_workflows import job, jobs, workflow_sources

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The field the fork fallback branches on. A sibling such as
#: ``head.repo.private`` is the correct-variant mutation: an expression that
#: reads it is well-formed and wrong, and a contract that merely pattern-matches
#: a conditional accepts it.
FORK_FIELD = "github.event.pull_request.head.repo.fork"

#: GitHub-hosted labels this repository is permitted to use. Named rather than
#: derived from a vendor prefix: a ``ubicloud-`` prefix filter would silently
#: exempt a second paid provider's labels from the registry question, which is
#: the substantive defect. The two agree over today's workflows and differ on
#: exactly the case that matters.
FROZEN_HOSTED_LABELS: typ.Final = (
    "macos-15-intel",
    "macos-latest",
    "ubuntu-20.04",
    "ubuntu-22.04",
    "ubuntu-latest",
    "windows-2022",
    "windows-latest",
)

# Surrounding whitespace is incidental: a folded scalar can leave a trailing
# space, and a lane written that way is correct. Without the `\s*` the whole
# declaration falls through to the literal branch, which is how a correctly
# written fork lane would quietly stop being read as one.
_EXPRESSION = re.compile(r"^\s*\$\{\{(?P<body>.*)\}\}\s*$", re.DOTALL)
#: An expression *fragment*, used to refuse a value that interpolates without
#: being wholly an expression.
_INTERPOLATION = re.compile(r"\$\{\{")
#: The canonical fork fallback, anchored end to end so the arms are read by
#: position rather than by membership. `\A`/`\Z` rather than a bare `fullmatch`
#: on a pattern that could otherwise be satisfied by a prefix.
_FORK_EXPRESSION = re.compile(
    r"\A\s*(?P<condition>[A-Za-z_][\w.]*)"
    r"\s*&&\s*'(?P<fork_arm>[^']+)'"
    r"\s*\|\|\s*'(?P<owned_arm>[^']+)'\s*\Z"
)
_MATRIX_EXPRESSION = re.compile(r"\A\s*matrix\.(?P<key>[\w-]+)\s*\Z")
#: Every ``${{ ... }}`` reference in a string, for the job-name stability rule.
_REFERENCE = re.compile(r"\$\{\{(?P<body>[^}]*)\}\}")
#: Expressions a job-level ``if:`` may not reduce to. A job that cannot run
#: satisfies every declaration-reading rule while gating nothing.
_NEVER_RUNS: typ.Final = frozenset({"false", "0", "'false'", '"false"'})


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


class Placement(typ.NamedTuple):
    """Where one job can run.

    Attributes
    ----------
    kind:
        ``literal``, ``fork`` or ``matrix``; the shape the declaration took.
    owned:
        The label selected when the fork arm is not taken, or ``None`` for a
        matrix placement, which has no single owned label. Cache lanes resolve
        through this, which is sound only because every save step in this
        repository is guarded on a push to ``refs/heads/main``, an event a fork
        can never produce. ``test_saves_happen_only_on_trunk_and_only_after_a_miss``
        holds that premise.
    fork:
        The label a fork's pull request takes, or ``None`` when the job
        declares no fallback.
    labels:
        Every label the declaration can resolve to.
    references:
        Every expression reference the declaration reads, resolved through the
        matrix values it selects.
    """

    kind: str
    owned: str | None
    fork: str | None
    labels: frozenset[str]
    references: frozenset[str]


def declares_steps(workflow_name: str, job_name: str) -> bool:
    """Report whether a job runs its own steps rather than calling a workflow.

    A job that declares ``uses`` is a reusable-workflow caller: it declares no
    runner of its own and GitHub rejects ``timeout-minutes`` on it, so placing
    and bounding it are the callee's business. Keying the exemption on ``uses``
    rather than on a missing ``runs-on`` means a job with neither is still
    refused (whitaker #438).

    Returns
    -------
    bool
        Whether the job declares its own steps and no ``uses`` key.
    """
    declared = job(workflow_name, job_name)
    return "uses" not in declared and isinstance(declared.get("steps"), list)


def all_jobs() -> list[tuple[str, str]]:
    """Return every job in the estate as a ``(workflow, job)`` pair.

    Partitioning matters more than it looks: filtering to the jobs a manifest
    recognizes sees nothing else, which is how an unclassified lane acquires a
    runner unreviewed.

    Returns
    -------
    list[tuple[str, str]]
        Every job in the estate, in workflow and declaration order.
    """
    return [
        (workflow_name, job_name)
        for workflow_name, _ in workflow_sources()
        for job_name in jobs(workflow_name)
    ]


def _matrix_values(workflow_name: str, job_name: str, key: str) -> list[object]:
    """Return the values one matrix key takes across the job's ``include``."""
    strategy = job(workflow_name, job_name).get("strategy")
    _require(
        condition=isinstance(strategy, dict),
        message=(
            f"{workflow_name}:{job_name} selects its runner from matrix.{key} "
            "but declares no strategy"
        ),
    )
    matrix = typ.cast("dict[str, object]", strategy).get("matrix")
    _require(
        condition=isinstance(matrix, dict),
        message=f"{workflow_name}:{job_name} strategy declares no matrix",
    )
    declared = typ.cast("dict[str, object]", matrix)
    # Two shapes reach here. A plain matrix lists the key's values directly
    # (`os: [ubuntu-latest, windows-2022]`); an `include` matrix lists legs and
    # the key sits inside each. Modelling only `include` refused
    # `rust-boundaries.yml:native` outright, which fails safe but rejects a
    # perfectly ordinary workflow.
    direct = declared.get(key)
    if isinstance(direct, list):
        return list(typ.cast("list[object]", direct))
    include = declared.get("include")
    _require(
        condition=isinstance(include, list),
        message=(
            f"{workflow_name}:{job_name} declares a matrix this reader cannot "
            f"expand; the key {key!r} is neither a list nor present in an "
            "`include` list"
        ),
    )
    legs = typ.cast("list[object]", include)
    values: list[object] = []
    for index, leg in enumerate(legs):
        _require(
            condition=isinstance(leg, dict) and key in leg,
            message=(
                f"{workflow_name}:{job_name} matrix leg {index} declares no "
                f"{key!r}, so its runner is unreadable"
            ),
        )
        values.append(typ.cast("dict[str, object]", leg)[key])
    return values


def _literal_labels(
    workflow_name: str, job_name: str, values: cabc.Iterable[object]
) -> frozenset[str]:
    """Narrow matrix runner values to literal labels, refusing expressions."""
    labels: set[str] = set()
    for value in values:
        _require(
            condition=isinstance(value, str) and _EXPRESSION.match(value) is None,
            message=(
                f"{workflow_name}:{job_name} resolves its runner to {value!r}; "
                "a matrix value holding an expression is a placement this "
                "reader cannot model, and recording it as a label would exempt "
                "the lane from every placement rule"
            ),
        )
        labels.add(str(value))
    return frozenset(labels)


def placement(workflow_name: str, job_name: str) -> Placement:
    """Model one job's runner selection, refusing any shape it cannot read.

    Returns
    -------
    Placement
        The shape read, its arms, every label it can resolve to, and every
        expression reference it reads.

    Raises
    ------
    AssertionError
        When the declaration is missing on a step-declaring job, is a list, is
        an expression in neither the fork nor the matrix shape, or parses with
        an embedded line break.
    """
    where = f"{workflow_name}:{job_name}"
    declared = job(workflow_name, job_name).get("runs-on")
    _require(
        condition=not isinstance(declared, list),
        message=(
            f"{where} declares a list runs-on. Each entry would be recorded as "
            "a label, so a runner group or an entry holding an expression would "
            "read as a job placed on two runners, one named after the "
            "expression (dev-env-rocky #216). Refused rather than modelled."
        ),
    )
    _require(
        condition=isinstance(declared, str) and bool(declared),
        message=f"{where} must declare a non-empty string runs-on, got {declared!r}",
    )
    text = str(declared)
    # The folded-scalar hazard. A continuation indented one level deeper keeps
    # its line break, and GitHub evaluates the broken value regardless, so a
    # green run is not evidence that the expression is one line.
    _require(
        condition="\n" not in text,
        message=(
            f"{where} runs-on parses with an embedded line break: {text!r}. "
            "Keep a folded-scalar continuation at the same indent."
        ),
    )
    match = _EXPRESSION.match(text)
    if match is None:
        # A value that interpolates without being wholly an expression, such as
        # `ubuntu-${{ matrix.release }}`, is not a literal label. Recording it
        # as one is the same defect this reader exists to prevent, wearing a
        # different shape: the label it resolves to at run time is invisible to
        # every placement and registry assertion.
        _require(
            condition=_INTERPOLATION.search(text) is None,
            message=(
                f"{where} interpolates its runner label: {text!r}. The labels "
                "this can resolve to are unreadable, so it is refused rather "
                "than recorded as one literal."
            ),
        )
        return Placement("literal", text, None, frozenset({text}), frozenset())
    body = match.group("body")
    fork = _FORK_EXPRESSION.match(body)
    if fork is not None:
        return Placement(
            "fork",
            fork.group("owned_arm"),
            fork.group("fork_arm"),
            frozenset({fork.group("owned_arm"), fork.group("fork_arm")}),
            frozenset({fork.group("condition")}),
        )
    matrix = _MATRIX_EXPRESSION.match(body)
    if matrix is not None:
        key = matrix.group("key")
        values = _matrix_values(workflow_name, job_name, key)
        return Placement(
            "matrix",
            None,
            None,
            _literal_labels(workflow_name, job_name, values),
            frozenset({f"matrix.{key}"}),
        )
    message = (
        f"{where} selects its runner from an expression this reader cannot "
        f"model: {text!r}. Recording it as one opaque label would carry no "
        "vendor prefix, so the lane would be dropped by every Ubicloud "
        "classifier while still asking for a paid runner (axinite #372)."
    )
    raise AssertionError(message)


def references(value: object) -> frozenset[str]:
    """Return every ``${{ ... }}`` reference a declaration reads."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(body.strip() for body in _REFERENCE.findall(value) if body.strip())


def never_runs(workflow_name: str, job_name: str) -> bool:
    """Report whether a job's own ``if:`` makes it unreachable.

    A constant-false guard leaves every declaration intact and runs nothing,
    which is exactly the state a placement contract must refuse rather than
    report as compliant.

    Returns
    -------
    bool
        Whether the job's own ``if:`` reduces to a constant false.
    """
    condition = job(workflow_name, job_name).get("if")
    if not isinstance(condition, str):
        return False
    text = " ".join(condition.split())
    expression = _EXPRESSION.match(text)
    if expression is not None:
        text = " ".join(expression.group("body").split())
    # A containment test would accept `false && matrix.target == 'x'`; the
    # leading clause is matched explicitly instead.
    return text.lower() in _NEVER_RUNS or text.lower().startswith("false &&")
