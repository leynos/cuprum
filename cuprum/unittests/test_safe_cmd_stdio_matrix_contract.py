"""The redirect ownership matrix is a contract, and this pins it.

``cuprum/unittests/test_safe_cmd_redirect.py`` states the matrix it covers in a
comment: ``{owned path, borrowed fd, borrowed file}`` crossed with ``{exit 0,
non-zero exit, timeout, spawn failure}``. A comment is not a guard. Two of those
cells live in sibling modules — the borrowed-file timeout and the borrowed-file
spawn failure, moved out when the redirect suite reached its cohesion ceiling —
and a case that is deleted, renamed, or quietly stopped from requesting the
backend axis would leave the comment still claiming coverage that no longer
exists.

So the matrix is written out here a second time, by hand, and each row is
checked against the modules that are supposed to hold it. The rows are not
derived from the test modules: a guard that read the cases and then asserted
what it had read would pass for any input, including an empty one. Written
independently, the two can disagree, and disagreement is the failure this
exists to report.

Each row is checked three times, because the checks catch different losses. The
attribute check reads ``pytestmark`` off the resolved function, which catches a
case whose backend request was removed — the loss CodeRabbit reported, and the
one a rename cannot hide. The collection check runs the suite and reads the node
ids back, which catches a case that stopped being collected at all: a marker
with a typo, a decorator that now skips it, or a name that no longer exists.

The third check reads the *parameter ids* of every collected row and compares
them against the combinations that row claims, because being collected is not
the same as being covered. A case that lost one execution strategy, or the
backend axis, is still collected under its own name — so only the combinations
can distinguish a cell that is evidence on both routes from one that is
evidence about whichever route the host preferred.
"""

from __future__ import annotations

import dataclasses as dc
import importlib
import inspect
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed pytest argv, no shell.
import sys
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

_REDIRECT = "cuprum.unittests.test_safe_cmd_redirect"
_BACKENDS = "cuprum.unittests.test_safe_cmd_spawn_failure_backends"
_BORROWED_FILE = "cuprum.unittests.test_safe_cmd_borrowed_file_outcomes"

# The two fixtures that select which spawn route a case runs through. Each is
# parameterized over both routes, so requesting either one is what stops a cell
# from being evidence about the route the host happened to prefer. The cells
# that fail *below* backend dispatch request ``refusing_backend_spawn`` because
# that fixture carries the injection for both routes; the rest request
# ``spawn_backend``. Naming them here rather than reading them from the modules
# is the point: the guard asserts what the matrix comment promises.
_ROUTE_AXES: typ.Final[frozenset[str]] = frozenset({
    "spawn_backend",
    "refusing_backend_spawn",
})
_SPAWN_BACKEND: typ.Final[str] = "spawn_backend"
_REFUSING_BACKEND: typ.Final[str] = "refusing_backend_spawn"
# A cell whose outcome is the shared dispatch failure has no route to select:
# the failure lands before the choice is made, for every route alike. It still
# has a strategy axis — the run API is crossed with both halves either way, so
# the cell is claimed for both — but it carries no route to be compared across.
_NO_ROUTE_AXIS: typ.Final[str] = ""


@dc.dataclass(frozen=True, slots=True)
class _MatrixRow:
    """One cell of the ownership matrix, and where it is covered.

    ``axis`` names the fixture the cell must request to select a spawn route.
    A cell that requests neither is evidence about whichever route the host
    happened to prefer — the property CodeRabbit reported as missing on five
    cases, each of which read as evidence about the matrix while being evidence
    about one route.
    """

    target: str
    outcome: str
    module: str
    case: str
    axis: str

    @property
    def label(self) -> str:
        """Describe the cell the way the matrix comment names it."""
        return f"{self.target} x {self.outcome}"


def _owned(
    outcome: str, module: str, case: str, *, axis: str = _SPAWN_BACKEND
) -> _MatrixRow:
    """Build an owned-path row."""
    return _MatrixRow("owned path", outcome, module, case, axis)


def _borrowed_fd(
    outcome: str, module: str, case: str, *, axis: str = _SPAWN_BACKEND
) -> _MatrixRow:
    """Build a borrowed-descriptor row."""
    return _MatrixRow("borrowed fd", outcome, module, case, axis)


def _borrowed_file(
    outcome: str, module: str, case: str, *, axis: str = _SPAWN_BACKEND
) -> _MatrixRow:
    """Build a borrowed-file row."""
    return _MatrixRow("borrowed file", outcome, module, case, axis)


# Written from the matrix comment, not read from the modules. Every row is a
# cell a reader of that comment would expect to find covered.
_MATRIX: typ.Final[tuple[_MatrixRow, ...]] = (
    # The owned path row. The success case is the descriptor-accounting one
    # rather than ``test_path_target_receives_the_children_bytes``: both assert
    # the child's bytes and a clean exit, and this one asserts the descriptor
    # accounting on top, so naming the weaker case would have claimed the cell
    # with less evidence than the module already holds. It also requests the
    # backend axis, which is what lets the cell be compared across routes
    # instead of being declared route-independent.
    _owned("exit 0", _REDIRECT, "test_path_target_does_not_leak_the_descriptor"),
    _owned("non-zero exit", _REDIRECT, "test_path_target_closed_after_a_failing_run"),
    _owned("timeout", _REDIRECT, "test_path_target_closed_after_a_timeout"),
    _owned(
        "shared spawn failure",
        _REDIRECT,
        "test_path_target_closed_after_a_spawn_failure",
        # The shared dispatch failure is the same whichever route would have
        # been chosen, so this cell has no route to select. The backend-specific
        # cells below are the ones that do.
        axis=_NO_ROUTE_AXIS,
    ),
    _owned(
        "backend spawn failure",
        _BACKENDS,
        "test_an_owned_path_is_closed_after_a_backend_spawn_failure",
        axis=_REFUSING_BACKEND,
    ),
    # The borrowed descriptor row.
    _borrowed_fd("exit 0", _REDIRECT, "test_borrowed_descriptor_survives_the_run"),
    _borrowed_fd(
        "non-zero exit", _REDIRECT, "test_borrowed_descriptor_survives_a_failing_run"
    ),
    _borrowed_fd("timeout", _REDIRECT, "test_borrowed_descriptor_survives_a_timeout"),
    _borrowed_fd(
        "shared spawn failure",
        _REDIRECT,
        "test_borrowed_descriptor_survives_a_spawn_failure",
        axis=_NO_ROUTE_AXIS,
    ),
    _borrowed_fd(
        "backend spawn failure",
        _BACKENDS,
        "test_a_borrowed_descriptor_survives_a_backend_spawn_failure",
        axis=_REFUSING_BACKEND,
    ),
    # The borrowed file object row. This is the row the redirect suite could not
    # hold in full; the two failure cells below are why its sibling module
    # exists.
    _borrowed_file(
        "exit 0", _REDIRECT, "test_borrowed_file_object_is_flushed_before_spawn"
    ),
    _borrowed_file(
        "non-zero exit",
        _REDIRECT,
        "test_borrowed_file_object_survives_a_failing_run",
    ),
    _borrowed_file(
        "timeout", _BORROWED_FILE, "test_borrowed_file_object_survives_a_timeout"
    ),
    _borrowed_file(
        "shared spawn failure",
        _BORROWED_FILE,
        "test_borrowed_file_object_survives_a_spawn_failure",
        axis=_NO_ROUTE_AXIS,
    ),
    _borrowed_file(
        "backend spawn failure",
        _BACKENDS,
        "test_a_borrowed_file_object_survives_a_backend_spawn_failure",
        axis=_REFUSING_BACKEND,
    ),
)

# The axes that must appear, as a set rather than as a count: a row replaced by
# a duplicate of another leaves the total intact.
_TARGETS: typ.Final[frozenset[str]] = frozenset({
    "owned path",
    "borrowed fd",
    "borrowed file",
})
_OUTCOMES: typ.Final[frozenset[str]] = frozenset({
    "exit 0",
    "non-zero exit",
    "timeout",
    "shared spawn failure",
    "backend spawn failure",
})


def _module(case: _MatrixRow) -> types.ModuleType:
    """Return the module a row says holds its case."""
    return importlib.import_module(case.module)


def _resolved(case: _MatrixRow) -> cabc.Callable[..., object]:
    """Return the test function a row names, or fail the assertion loudly."""
    module = _module(case)
    try:
        return getattr(module, case.case)
    except AttributeError as error:
        message = (
            f"{case.module} no longer defines {case.case!r}, so the "
            f"{case.label} cell of the matrix is uncovered"
        )
        raise AssertionError(message) from error


def _asked_of(function: cabc.Callable[..., object]) -> set[str]:
    """Return the fixture names this callable asks pytest for directly.

    Both request routes count. A fixture that returns a value is asked for by
    naming it as a parameter; one used only for its side effect is asked for
    through ``@pytest.mark.usefixtures``. Reading only one of them would
    report a covered case as deficient, which is how an earlier version of
    this guard failed.

    Returns
    -------
    set[str]
        The fixture names requested here, not including anything those
        fixtures request in turn.
    """
    requested: set[str] = set()
    for mark in getattr(function, "pytestmark", []):
        if mark.name == "usefixtures":
            requested.update(mark.args)
    requested.update(inspect.signature(function).parameters)
    return requested


# How far the search below follows one fixture to another. The deepest chain in
# this matrix is three links — a case asks for ``backend_failure_case``, which
# asks for ``refusing_backend_spawn`` — so four is slack, not a tuning knob.
_ROUTE_SEARCH_DEPTH: typ.Final[int] = 4


def _route_axes(
    function: cabc.Callable[..., object], module: types.ModuleType
) -> set[str]:
    """Return the route axes a case reaches, directly or through a fixture.

    Following the graph matters because the two ways of reaching an axis are
    both legitimate. The non-zero-exit and timeout cells name
    ``spawn_backend`` in their own signature or marker; the backend-specific
    cells name ``backend_failure_case`` and reach ``refusing_backend_spawn``
    one link further on. Inspecting only the case's own request would call
    three covered cells uncovered — and, worse, would be satisfied by a case
    that requested the axis and then never used it.

    Returns
    -------
    set[str]
        The route axes reached within ``_ROUTE_SEARCH_DEPTH`` links.
    """
    found: set[str] = set()
    seen: set[str] = set()
    frontier = [function]
    for _ in range(_ROUTE_SEARCH_DEPTH):
        asked = {name for holder in frontier for name in _asked_of(holder)}
        found |= asked & _ROUTE_AXES
        # Names that do not resolve in the case's own module are pytest
        # builtins such as ``request`` or ``monkeypatch``, which cannot lead
        # to a route axis and are not worth following.
        pending = sorted(
            name
            for name in asked - _ROUTE_AXES
            if name not in seen and hasattr(module, name)
        )
        if not pending:
            break
        seen.update(pending)
        frontier = [getattr(module, name) for name in pending]
    return found


def _collected_node_ids(*modules: str) -> list[str]:
    """Collect the named modules in a subprocess and return their node ids."""
    paths = [f"{module.replace('.', '/')}.py" for module in modules]
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *paths,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "collecting the redirect matrix modules failed, so this guard cannot "
        f"say what is covered:\n{completed.stdout}\n{completed.stderr}"
    )
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and line.strip()
    ]


def test_the_matrix_has_the_axes_it_claims() -> None:
    """Every combination of the two axes is named at least once."""
    covered = {(row.target, row.outcome) for row in _MATRIX}
    assert {target for target, _ in covered} == _TARGETS, (
        "the owned, borrowed-descriptor, and borrowed-file rows must all be "
        f"present; found {sorted({target for target, _ in covered})}"
    )
    assert {outcome for _, outcome in covered} == _OUTCOMES, (
        "each ownership row must cover the successes and every way a run can "
        f"fail; found {sorted({outcome for _, outcome in covered})}"
    )
    missing = {
        (target, outcome) for target in _TARGETS for outcome in _OUTCOMES
    } - covered
    assert not missing, f"the matrix has uncovered cells: {sorted(missing)}"


def test_every_row_selects_the_route_it_claims() -> None:
    """A row must request the route axis it names, and only a real one."""
    deficient = [
        f"{row.label} (expected {row.axis!r})"
        for row in _MATRIX
        if row.axis and row.axis not in _route_axes(_resolved(row), _module(row))
    ]
    assert not deficient, (
        "these matrix cells do not request the route axis they claim, so each "
        f"is evidence about one spawn route only: {deficient}"
    )
    unexpected = [
        row.label
        for row in _MATRIX
        if not row.axis and _route_axes(_resolved(row), _module(row))
    ]
    assert not unexpected, (
        "these cells name no route axis but request one anyway, so the row "
        f"table and the modules disagree about what they cover: {unexpected}"
    )
    # Non-vacuity: some rows must select a route, or the assertion above holds
    # for a matrix in which every cell was silently declared route-independent.
    selecting = [row.label for row in _MATRIX if row.axis]
    assert selecting, (
        "no matrix row selects a spawn route at all, so the assertion above "
        "cannot fail and proves nothing"
    )


def test_every_matrix_row_is_collected() -> None:
    """A row that is no longer collected has stopped being a test.

    The attribute check above reads a function that still exists; this one
    reads what the suite would actually run. A case hidden behind a marker
    typo, or renamed out from under its row, passes the first and fails here.
    """
    modules = sorted({row.module for row in _MATRIX})
    collected = _collected_node_ids(*modules)
    missing = [
        row.label for row in _MATRIX if not any(row.case in node for node in collected)
    ]
    assert not missing, (
        f"these matrix cells name cases the suite no longer collects: {missing}"
    )
    # Non-vacuity: the collection must be able to see the rows at all, so a
    # collector that returned nothing cannot pass the check above.
    assert len(collected) > len(_MATRIX), (
        "the collected node list is no larger than the row list, which means "
        f"collection returned almost nothing: {len(collected)} nodes"
    )


# The number of execution strategies and the number of spawn routes are written
# out here rather than counted from the fixtures. A guard that counted what the
# fixtures happened to offer would agree with them however they changed, which
# is the failure it exists to catch.
_STRATEGIES: typ.Final[frozenset[str]] = frozenset({"run()", "run_sync()"})
_ROUTES: typ.Final[frozenset[str]] = frozenset({"wait4", "asyncio"})


def _axis_combinations(case: str, collected: cabc.Sequence[str]) -> frozenset[str]:
    """Return the axis combinations one case's collected ids offer.

    Reading combinations rather than names is the point. A case that stopped
    requesting one execution strategy, or that lost the backend axis, still
    appears under its name — so a substring check against the collected ids
    passes while half the cell's evidence is gone. What the matrix claims is
    not "this case exists" but "this cell is covered on both strategies and
    both routes", and only the parameter ids can witness that.

    Parameters
    ----------
    case : str
        The bare test name, matched against the node id exactly so that a case
        whose name is a suffix of another cannot borrow its ids.
    collected : collections.abc.Sequence[str]
        The node ids collected for the module that holds this case. Passed in
        rather than collected here, so one module's ids are read once and every
        row that lives in it is compared against the same list.

    Returns
    -------
    frozenset[str]
        One normalized combination per collected id for this case, each built
        by sorting the ``-``-separated parameter ids so the result does not
        depend on the module's fixture request order. Empty when the case is
        not collected at all.
    """
    found: set[str] = set()
    for node in collected:
        _, _, rest = node.partition("::")
        name, _, params = rest.partition("[")
        if name != case or not params:
            continue
        # Param order follows the fixture request order, which differs between
        # modules — ``[wait4-run()]`` in one and ``[run()-asyncio]`` in
        # another — so the parameters are split and matched as a set.
        parts = {part for part in params.rstrip("]").split("-") if part}
        found.add("-".join(sorted(parts)))
    return frozenset(found)


def _claimed_combinations(row: _MatrixRow) -> frozenset[str]:
    """Return the axis combinations one row claims it is collected under.

    Every row claims both execution strategies. The route is claimed only where
    the row names an axis to select it with: a cell whose outcome is the shared
    dispatch failure has no route, and claiming one for it would demand a
    parameter that no fixture can supply.

    Returns
    -------
    frozenset[str]
        The exact set of normalized combinations this row must be collected
        under, for comparison as a set rather than as a count.
    """
    routes = _ROUTES if row.axis else frozenset({""})
    return frozenset(
        "-".join(sorted(part for part in (strategy, route) if part))
        for strategy in _STRATEGIES
        for route in routes
    )


def test_every_backed_case_is_collected_on_both_strategies_and_routes() -> None:
    """Every matrix cell is collected on every combination it claims.

    The name check above cannot see this. ``test_borrowed_descriptor_survives_a
    _timeout`` is collected under four combinations when the backend axis is
    requested and under two when it is not, and both lists contain the name —
    so only a comparison of the combinations distinguishes a cell that is
    evidence on both routes from one that is evidence on whichever route the
    host preferred.

    Every module is compared, not just the redirect suite. The two sibling
    modules hold cells the matrix claims, so excluding them left seven of the
    fifteen cells checked by name alone — a case there could have lost a route
    or an execution strategy and still passed. The comparison is per module
    because the *expected* set is built from the rows, not from any module's
    parametrization order, so nothing is imported across module boundaries.

    This is also why the check is written as an exact expected set rather than
    as a count: a case that lost one route and gained a duplicate of another
    would keep its total.
    """
    collected_by_module = {
        module: _collected_node_ids(module)
        for module in sorted({row.module for row in _MATRIX})
    }
    compared: set[tuple[str, str]] = set()
    deficient: list[str] = []
    for row in _MATRIX:
        compared.add((row.target, row.outcome))
        found = _axis_combinations(row.case, collected_by_module[row.module])
        expected = _claimed_combinations(row)
        if found != expected:
            deficient.append(
                f"{row.label} ({row.case}): {sorted(found)} "
                f"rather than {sorted(expected)}"
            )
    # The population is the loop's own record of what it compared, checked
    # against the cells the matrix contract names rather than against the table
    # the loop was handed. A row deleted from the table shrinks both the loop
    # and any set derived from it, so a pin built from ``_MATRIX`` would shrink
    # with it and report nothing; ``_TARGETS`` and ``_OUTCOMES`` are written out
    # independently at module level, which is what makes them able to disagree.
    # This is checked before the deficiencies so that a guard which compared too
    # little fails on that, rather than reporting a clean run over a subset.
    asserted = {(target, outcome) for target in _TARGETS for outcome in _OUTCOMES}
    assert compared == asserted, (
        "the guard must compare every matrix cell; comparing "
        f"{sorted(compared)} would leave {sorted(asserted - compared)} uncovered"
    )
    assert not deficient, (
        "these matrix cells are not collected on every execution strategy and "
        f"spawn route they claim, so each is partial evidence: {deficient}"
    )


def test_the_backend_axis_supplies_both_routes() -> None:
    """The axis the rows request is the two-route fixture, not a stub."""
    module = importlib.import_module(_REDIRECT)
    for target, outcome in (("owned path", "exit 0"), ("borrowed fd", "exit 0")):
        row = next(
            row for row in _MATRIX if (row.target, row.outcome) == (target, outcome)
        )
        assert row.axis == _SPAWN_BACKEND, (
            f"the {target} success cell must request {_SPAWN_BACKEND!r} like "
            f"every other backed cell, or it is evidence about one route; the "
            f"row table says {row.axis!r}"
        )
        requested = _asked_of(getattr(module, row.case))
        assert _SPAWN_BACKEND in requested, (
            f"{row.case!r} is claimed for both spawn routes, but it does not "
            "request the fixture that offers them; the row table and the "
            "module disagree"
        )
    for axis in sorted(_ROUTE_AXES):
        fixture = getattr(module, axis, None) or getattr(
            importlib.import_module(_BACKENDS), axis
        )
        marker = fixture._fixture_function_marker
        assert list(marker.params) == ["wait4", "asyncio"], (
            f"the {axis!r} axis must still offer both spawn routes, or every "
            f"row requesting it is evidence about one route; got {marker.params!r}"
        )
