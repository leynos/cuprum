"""Contract tests for the Make targets that run the workflow harness.

`tests/test_ci_workflow_harness_job.py` checks that the opt-in CI job invokes
`make test-act`. That leaves the target's own recipe unguarded, and every way
it can go wrong fails *quietly*:

- drop `CUPRUM_REQUIRE_ACT=1` and a machine with no container runtime turns
  every scenario into a skip, so the target exits zero for having run nothing —
  the job is green and the boundary was never exercised;
- drop `$(ACT_SCENARIO_TARGETS)` and pytest collects nothing, which also exits
  zero;
- add the scenario target to `PYTEST_TARGETS` and `make test` starts requiring
  a container runtime, so the default suite fails on a machine that cannot run
  the scenarios at all;
- drop the parser target from `PYTEST_TARGETS` and the recorded-stream parsing
  silently stops running anywhere, in either suite.

None of these is visible from a green run, and the workflow-side test cannot
see them: it reads the workflow's invocation of the target, not the target.
This module reads the Makefile itself.

The assertions are structural rather than byte-exact. A recipe is matched by
the variable it expands, so reordering prerequisites or adding a flag does not
fail the test, while removing the variable or the target does.
"""

from __future__ import annotations

import re
import shutil
import typing as typ

from tests.helpers.act_runtime import docker_host, harness_skip_reason
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import pytest

#: The variable that turns a skip into a failure, and the value it must carry.
#: `harness_skip_reason()` compares it to exactly `"1"`, so `true` or `yes`
#: would read as unset and restore the silent-skip behaviour.
REQUIRE_ACT_ENV = "CUPRUM_REQUIRE_ACT"
REQUIRED_VALUE = "1"

#: The two halves of the harness, and the file each must still name. The
#: parser half is string handling over recorded `act` output and belongs in the
#: default suite; the scenario half needs a container runtime and must not be.
TARGET_LISTS = {
    "ACT_PARSER_TARGETS": "tests/integration/test_act_stream_parsing.py",
    "ACT_SCENARIO_TARGETS": "tests/integration/test_workflow_integration.py",
}

#: Recipe text that runs the suite. The target uses the `PYTEST` variable; a
#: recipe spelling the command out is equally valid, so both are accepted.
_PYTEST_INVOCATION = re.compile(r"pytest|\$\(PYTEST\)", re.IGNORECASE)


def _makefile() -> str:
    """Return the repository Makefile as text.

    Returns
    -------
    str
        The Makefile's UTF-8 contents.
    """
    return (repo_root() / "Makefile").read_text(encoding="utf-8")


def _directives(source: str) -> str:
    """Return the Makefile's non-comment lines, joined.

    Comments are removal targets for these contracts — `CUPRUM_REQUIRE_ACT` is
    named in the prose above `test-act` precisely to explain why the recipe
    sets it — so a test counting occurrences must read the directives alone.
    Otherwise a better comment breaks the assertion that guards the recipe.

    Parameters
    ----------
    source : str
        Makefile source text.

    Returns
    -------
    str
        Every line that is not a comment, newline-joined.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


def _variable(name: str) -> str:
    """Return a Makefile variable's value, joining its continuation lines.

    Parameters
    ----------
    name : str
        Variable name, without the trailing assignment operator.

    Returns
    -------
    str
        The assigned value with backslash-newline continuations collapsed to
        single spaces, so a multi-line list compares as one string.

    Raises
    ------
    AssertionError
        If the variable is not assigned in the Makefile.
    """
    match = re.search(
        rf"^{name}\s*[?:]?=\s*(.*?)(?=\n\S|\Z)",
        _directives(_makefile()),
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        message = f"the Makefile must assign {name}"
        raise AssertionError(message)
    return re.sub(r"\\\n\s*", " ", match.group(1)).strip()


def _target(name: str) -> str:
    """Return a Makefile target's recipe.

    Parameters
    ----------
    name : str
        Target name.

    Returns
    -------
    str
        The recipe's lines, up to the first blank line.

    Raises
    ------
    AssertionError
        If the target is not declared in the Makefile.
    """
    source = _directives(_makefile())
    marker = f"\n{name}:"
    if marker not in source:
        message = f"the Makefile must declare a {name} target"
        raise AssertionError(message)
    recipe = source.split(marker, 1)[1].split("\n\n", 1)[0]
    return "\n".join(line for line in recipe.splitlines() if line.startswith("\t"))


def test_the_act_target_executes_the_scenarios_through_pytest() -> None:
    """Run the scenario target itself, not a bare directory or no target.

    What the recipe *runs* is the assertion: pytest on
    `$(ACT_SCENARIO_TARGETS)`. Matching the variable expansion rather than a
    literal path is deliberate — it is the same variable the exclusion test
    reads, so a scenario file added to the variable is collected here too.
    """
    recipe = _target("test-act")
    assert _PYTEST_INVOCATION.search(recipe), (
        "`test-act` must run pytest; it is the only target that runs the "
        f"container scenarios. Recipe: {recipe!r}"
    )
    assert "$(ACT_SCENARIO_TARGETS)" in recipe.split(), (
        "`test-act` must pass $(ACT_SCENARIO_TARGETS) to pytest. A recipe "
        "that resolves to an empty list still exits zero, so the scenarios "
        "would stop running without failing anything."
    )


def test_the_act_target_refuses_a_skip() -> None:
    """Set `CUPRUM_REQUIRE_ACT=1`, which is what makes a skip a failure.

    The scenarios skip where no container runtime is present. Without this
    variable a job that provides a runtime would still report success on the
    day it silently stopped working, and a suite that skips is
    indistinguishable from a suite that passes. The value is pinned because
    `harness_skip_reason()` compares it to exactly `"1"`.
    """
    recipe = _target("test-act")
    assert re.search(rf"\b{REQUIRE_ACT_ENV}={REQUIRED_VALUE}\b", recipe), (
        f"`test-act` must set {REQUIRE_ACT_ENV}={REQUIRED_VALUE}; without it "
        f"every scenario may skip and the target still exits zero. Recipe: "
        f"{recipe!r}"
    )


def test_the_act_target_is_the_only_place_the_refusal_is_set() -> None:
    """Keep the refusal tied to the command that consumes it.

    `CUPRUM_REQUIRE_ACT` is read by the harness at collection time. If a
    second recipe ran the scenarios without setting it, the refusal would be a
    property of one target rather than of running the suite, and that recipe
    would reinstate the silent skip. One setting means one route in.
    """
    directives = _directives(_makefile())
    assert directives.count(REQUIRE_ACT_ENV) == 1, (
        f"{REQUIRE_ACT_ENV} must appear in exactly one directive — the "
        "`test-act` recipe — so the refusal cannot drift from the command "
        "that sets it"
    )
    assert REQUIRE_ACT_ENV in _target("test-act"), (
        "the single setting must be in `test-act`; found it elsewhere"
    )


def test_scenario_targets_stay_out_of_the_default_suite() -> None:
    """Keep the container-bound scenarios out of `make test`.

    `make test` runs on every machine, and `make test-python` is what CI runs
    on the jobs that have no container runtime. Collecting the scenarios there
    would either fail those jobs or silently make the default suite require a
    runtime it promises not to need.
    """
    included = _variable("PYTEST_TARGETS")
    scenarios = _variable("ACT_SCENARIO_TARGETS")
    assert scenarios, "ACT_SCENARIO_TARGETS must name the scenario suite"
    assert "$(ACT_SCENARIO_TARGETS)" not in included, (
        "PYTEST_TARGETS must not expand ACT_SCENARIO_TARGETS, or `make test` "
        "starts requiring a container runtime"
    )
    for path in scenarios.split():
        assert path not in included, (
            f"{path} must not be listed in PYTEST_TARGETS, directly or "
            f"through a glob; `make test` would collect it"
        )


def test_parser_targets_stay_in_the_default_suite() -> None:
    """Keep the recorded-stream parser tests running everywhere.

    The parser tests need no runtime — their input is a fixture under
    `tests/fixtures/` — so the exclusion above must not be implemented by
    dropping the whole harness from the default suite. If it were, a change to
    `act`'s output format would only surface in the opt-in job.
    """
    assert "$(ACT_PARSER_TARGETS)" in _variable("PYTEST_TARGETS"), (
        "PYTEST_TARGETS must run the parser half of the harness; the "
        "exclusion of the scenario half must not remove both"
    )


def test_the_two_halves_do_not_overlap() -> None:
    """Keep the parser and scenario lists disjoint.

    A file in both lists runs in the default suite *and* in `test-act`, which
    is the duplicate-execution problem the repository pins elsewhere: the
    second run gates nothing and costs runner minutes. It also makes the
    exclusion vacuous for that file.
    """
    parser = set(_variable("ACT_PARSER_TARGETS").split())
    scenarios = set(_variable("ACT_SCENARIO_TARGETS").split())
    assert not parser & scenarios, (
        "a target in both harness lists runs twice and defeats the exclusion; "
        f"found {sorted(parser & scenarios)}"
    )


def test_the_named_targets_exist() -> None:
    """Resolve both halves to real files, so a rename cannot pass unnoticed.

    The variables are only strings until something reads them. Asserting the
    named paths exist turns "the variable still mentions something" into "the
    variable still names the suite", which is the failure a rename produces:
    the glob expands to nothing and pytest exits zero.
    """
    root = repo_root()
    for name, expected in TARGET_LISTS.items():
        declared = _variable(name)
        assert expected in declared.split(), (
            f"{name} must still name {expected}; found {declared!r}"
        )
        assert (root / expected).is_file(), f"{expected} must exist in the tree"


# -- runtime endpoint selection ----------------------------------------------

#: An endpoint no local probe could discover, and the configuration the harness
#: previously could not run: a remote daemon named by the caller.
_REMOTE_ENDPOINT = "tcp://dockerd.example.invalid:2375"


def test_a_configured_runtime_endpoint_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honour a caller-supplied `DOCKER_HOST` instead of replacing it.

    The harness passes `DOCKER_HOST` into `act`'s environment. Overwriting a
    value the caller set redirects the run to a local socket — a different
    daemon, silently, with the scenarios reporting on whatever it serves.
    Refusing to run because no *local* socket exists is the other half of the
    same mistake: it rejects a configuration that would have worked. The
    endpoint may be remote, in which case no probe here can see it at all, so
    the configured value has to be authoritative and returned unprobed.
    """
    monkeypatch.setenv("DOCKER_HOST", _REMOTE_ENDPOINT)
    assert docker_host() == _REMOTE_ENDPOINT, (
        "a configured endpoint must be used as-is; replacing it silently runs "
        "the scenarios against a different daemon"
    )


def test_a_configured_runtime_endpoint_satisfies_the_skip_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not skip a run whose runtime the caller has already named.

    The probe answers "can this host run the harness". A configured endpoint is
    the caller's answer to the part of that question about a socket, and it may
    name a daemon this host cannot reach directly — so testing the local sockets
    instead would skip every scenario on a host that is ready to run them.

    Both directions are asserted, because either alone can pass vacuously. On a
    host that happens to *have* a local socket, "the probe returns no reason"
    is true whatever the endpoint logic does. With the candidate sockets
    removed, the configured endpoint is the only thing that can satisfy the
    probe, and the unconfigured case must still be refused — so a probe that
    ignored the endpoint, and one that skipped unconditionally, both fail here.
    """
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("tests.helpers.act_runtime._socket_paths", lambda: ())

    monkeypatch.setenv("DOCKER_HOST", _REMOTE_ENDPOINT)
    assert harness_skip_reason() == "", (
        "a configured endpoint must satisfy the socket half of the probe; "
        "probing local sockets would skip a run that could have proceeded"
    )
    assert docker_host() == _REMOTE_ENDPOINT, (
        "the probe and the endpoint chooser must agree about the endpoint, or "
        "a scenario runs against an endpoint the probe never accepted"
    )

    monkeypatch.delenv("DOCKER_HOST")
    assert harness_skip_reason() != "", (
        "with no endpoint configured and no candidate socket, the harness must "
        "still report a skip reason rather than attempting a run it cannot make"
    )
