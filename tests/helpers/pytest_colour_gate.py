"""Drive the colour-pinned `test-python` recipe with stubbed tools.

`tests/test_ci_pytest_colour_contract.py` asserts that every Python gate
session runs under a fixed colour environment. The driving machinery lives
here so that module stays a set of assertions and each unit here keeps a
single responsibility.

A stubbed `uv` records what each session was given and never starts pytest, so
a driven run needs no virtual environment and costs one short process per
session. The stub also starts a child with no `env=` argument: exporting the
policy is only useful if an ordinary child, the kind a test spawns without
replacing the environment, inherits it.

The recipes run with their prerequisite targets excluded, so nothing builds,
syncs, or probes a host tool. The Makefile is read only to learn which
conditional variables a caller could have exported; those are scrubbed from
the child environment so a suite launched as `make test PYTEST_TARGETS=…`
cannot have the caller's list reported as though the Makefile declared it.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, drives the local Makefile.
import sys
import textwrap
import typing as typ

from tests.helpers.ci_documents import require
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The variables that ask an interpreter or tool for colour. The gate must
#: remove all three; `PY_COLORS` is unset rather than zeroed because pytest
#: gives it the highest precedence of any colour control.
FORCE_VARIABLES: typ.Final = ("FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS")

#: The values the gate must export. `NO_COLOR` also covers an interpreter
#: started with `-E`, which ignores every `PYTHON*` variable.
PINNED_VARIABLES: typ.Final = {"NO_COLOR": "1", "PYTHON_COLORS": "0"}

#: A name no recipe knows. Its survival shows the prefix clears only the
#: colour controls rather than rebuilding the environment.
SENTINEL_NAME: typ.Final = "CUPRUM_COLOUR_SENTINEL"
SENTINEL_VALUE: typ.Final = "kept"

#: A driven run only spawns the stub, so the budget is generous rather than
#: tight; the test's own pytest-timeout marker sits above it.
GATE_TIMEOUT_SECONDS: typ.Final = 120

#: Matches a Makefile variable declared with `?=`, which assigns only when the
#: name is not already defined — and a name in the environment counts as
#: defined. `make` exports its own command-line overrides under that name, so a
#: suite run as `make test PYTEST_TARGETS=…` would otherwise have the nested
#: `make` report the caller's list as though the Makefile declared it.
_CONDITIONAL_ASSIGNMENT: typ.Final = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\?=", re.MULTILINE
)

#: `make`'s own bookkeeping, which leaks in when pytest is launched from
#: `make test`; an inherited jobserver flag makes the nested `make` warn.
_MAKE_BOOKKEEPING: typ.Final = frozenset({"MAKEFLAGS", "MAKELEVEL", "MFLAGS"})

#: The stub's recorder. It accepts only the `uv run pytest` shape the recipes
#: use, records the environment the session was given, starts a child with no
#: `env=` argument so the child report shows what an ordinary spawn inherits,
#: and never starts pytest.
_RECORDER_SOURCE: typ.Final = textwrap.dedent(
    """\
    import json
    import os
    import pathlib
    import subprocess
    import sys

    VARIABLES = (
        "FORCE_COLOR",
        "NO_COLOR",
        "PYTHON_COLORS",
        "PY_COLORS",
        "CLICOLOR_FORCE",
        "CUPRUM_COLOUR_SENTINEL",
    )

    def report():
        return {name: os.environ.get(name) for name in VARIABLES}

    def child_report():
        reporter = pathlib.Path(__file__).with_name("report_environment.py")
        completed = subprocess.run(
            [sys.executable, str(reporter)],
            capture_output=True,
            check=False,
            text=True,
        )
        completed.check_returncode()
        return json.loads(completed.stdout)

    argv = sys.argv[1:]
    if argv[:2] != ["run", "pytest"]:
        raise SystemExit(64)
    record = {
        "argv": argv[2:],
        "environment": report(),
        "child": child_report(),
    }
    with pathlib.Path(os.environ["PYTEST_COLOUR_LOG"]).open("a") as log:
        print(json.dumps(record), file=log)
    """
)

#: The child the recorder starts. It is a separate file so the recorder can
#: invoke it with no `env=` argument, which makes the report evidence about
#: inheritance rather than about a mapping the recorder rebuilt.
_REPORTER_SOURCE: typ.Final = textwrap.dedent(
    """\
    import json
    import os

    VARIABLES = (
        "FORCE_COLOR",
        "NO_COLOR",
        "PYTHON_COLORS",
        "PY_COLORS",
        "CLICOLOR_FORCE",
        "CUPRUM_COLOUR_SENTINEL",
    )

    print(json.dumps({name: os.environ.get(name) for name in VARIABLES}))
    """
)


class UvCall(typ.TypedDict):
    """One environment report recorded by the stubbed `uv`."""

    argv: list[str]
    environment: dict[str, str | None]
    child: dict[str, str | None]


class SelectorFiles(typ.NamedTuple):
    """The individual and grouped selector files a driven run declares."""

    individual: Path
    grouped: Path


def gate_skip_reason() -> str:
    """Return why this host cannot drive the gate's recipes, if it cannot."""
    if os.environ.get("OS") == "Windows_NT":
        return "the gate's recipes run under a POSIX shell; this is Windows_NT"
    make = shutil.which("make")
    if make is None:
        return "the gate's recipes are Make targets; this host provides no make"
    if shutil.which("sh") is None:
        return "the gate's recipes run under a POSIX shell; this host has no sh"
    version = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [make, "--version"],
        capture_output=True,
        check=False,
        text=True,
    )
    if "GNU Make" not in version.stdout:
        return "the recipes under test use GNU Make's -o option; this make is not GNU"
    return ""


@functools.cache
def _caller_owned_names() -> frozenset[str]:
    """Return environment names that would redefine a Makefile variable."""
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    overridable = frozenset(_CONDITIONAL_ASSIGNMENT.findall(makefile))
    require(
        condition=bool(overridable),
        message=(
            "no `?=` assignments were found in the Makefile, so this scrub "
            "has stopped protecting anything; check the pattern still matches"
        ),
    )
    return overridable | _MAKE_BOOKKEEPING


def write_stub(bin_directory: Path) -> None:
    """Write the stub `uv`, its recorder, and the child reporter.

    The `uv` stub is a POSIX shell wrapper so the recipes' own `PATH` prefix
    resolves it, and it hands straight to the absolute interpreter running this
    test, independent of whichever `uv` and interpreter `PATH` would select.
    """
    recorder = bin_directory / "record_uv_call.py"
    recorder.write_text(_RECORDER_SOURCE, encoding="utf-8")
    (bin_directory / "report_environment.py").write_text(
        _REPORTER_SOURCE, encoding="utf-8"
    )
    stub = bin_directory / "uv"
    stub.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def write_selectors(tmp_path: Path) -> SelectorFiles:
    """Create the empty selector files a driven run declares.

    Returns
    -------
    SelectorFiles
        The individual and grouped selector files.
    """
    # `PYTEST_GROUPED_DESELECT` passes the grouped target unquoted, and `make`
    # splits the list on whitespace, so the assertion below states the fixture
    # assumption rather than leaving it to a confusing failure downstream.
    selectors = SelectorFiles(
        individual=tmp_path / "individual_case.py",
        grouped=tmp_path / "grouped_case.py",
    )
    for selector in selectors:
        selector.write_text("", encoding="utf-8")
        require(
            condition=not any(character.isspace() for character in str(selector)),
            message=(
                "the recipe reads selectors as bare words, so a path with "
                f"whitespace would change the command: {str(selector)!r}"
            ),
        )
    return selectors


def read_calls(log: Path) -> list[UvCall]:
    """Read the stub's JSON-lines transcript in the order sessions ran."""
    return [
        typ.cast("UvCall", json.loads(line))
        for line in log.read_text(encoding="utf-8").splitlines()
    ]


def _gate_selections(
    bin_directory: Path, selectors: SelectorFiles, *, override_pytest: bool
) -> list[str]:
    """Return the command-line assignments that stub the recipe's tools."""
    tool_path = f"{bin_directory}{os.pathsep}{os.environ.get('PATH', '')}"
    selections = [
        f"LOCAL_TOOL_PATH={tool_path}",
        f"PYTEST_TARGETS={selectors.individual} {selectors.grouped}",
        f"PYTEST_GROUPED_TARGETS={selectors.grouped}",
        "PYTEST_WORKERS=0",
    ]
    if override_pytest:
        selections.append(f"PYTEST={bin_directory / 'uv'} run pytest")
    return selections


def _gate_environment(log: Path, home: Path) -> dict[str, str]:
    """Return the hostile environment the gate is driven under."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _caller_owned_names()
    }
    environment.update({
        "HOME": str(home),
        "PYTEST_COLOUR_LOG": str(log),
        "FORCE_COLOR": "1",
        "PYTHON_COLORS": "1",
        "PY_COLORS": "1",
        "CLICOLOR_FORCE": "1",
        "NO_COLOR": "",
        SENTINEL_NAME: SENTINEL_VALUE,
    })
    return environment


def _run_gate(selections: list[str], environment: dict[str, str]) -> None:
    """Run `test-python` with every build prerequisite excluded."""
    make = shutil.which("make") or ""
    require(
        condition=bool(make),
        message=(
            "the caller's skip marker only clears when `make` exists, so its "
            "absence here means the host changed mid-run"
        ),
    )
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [
            make,
            "-j1",
            "-o",
            "build",
            "-o",
            "uv",
            "-o",
            "pytest",
            "-o",
            "ruff",
            "-o",
            "makeutil",
            "test-python",
            *selections,
        ],
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env=environment,
        text=True,
        timeout=GATE_TIMEOUT_SECONDS,
    )
    output = f"{completed.stdout}{completed.stderr}"
    require(
        condition=completed.returncode == 0,
        message=(
            "`make test-python` must run every session under the colour "
            f"policy; it exited {completed.returncode}. Output:\n{output}"
        ),
    )


def drive_gate(
    tmp_path: Path, *, override_pytest: bool
) -> tuple[list[UvCall], SelectorFiles]:
    """Drive `test-python` with stubbed tools under a hostile colour environment.

    Parameters
    ----------
    tmp_path : Path
        The test's temporary directory; every stub and selector lives beneath
        it.
    override_pytest : bool
        Whether to replace `PYTEST` on the command line with the stub. The
        policy prefix sits outside that variable, so both cases must produce
        the same reports.

    Returns
    -------
    tuple[list[UvCall], SelectorFiles]
        The recorded sessions in call order, and the selector files the run
        declared.
    """
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    write_stub(bin_directory)
    log = tmp_path / "uv-calls.jsonl"
    log.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    selectors = write_selectors(tmp_path)
    selections = _gate_selections(
        bin_directory, selectors, override_pytest=override_pytest
    )
    _run_gate(selections, _gate_environment(log, home))
    return read_calls(log), selectors


def assert_policy(observed: dict[str, str | None], *, subject: str) -> None:
    """Assert one recorded environment carries the gate's colour policy."""
    for variable in FORCE_VARIABLES:
        require(
            condition=observed.get(variable) is None,
            message=(
                f"{subject} must not carry {variable}; the gate removes the "
                f"force-colour variables and got {observed.get(variable)!r}. "
                f"Report: {observed!r}"
            ),
        )
    for variable, value in PINNED_VARIABLES.items():
        require(
            condition=observed.get(variable) == value,
            message=(
                f"{subject} must carry {variable}={value}, got "
                f"{observed.get(variable)!r}. Report: {observed!r}"
            ),
        )
    require(
        condition=observed.get(SENTINEL_NAME) == SENTINEL_VALUE,
        message=(
            f"{subject} must leave unrelated variables alone, got "
            f"{observed.get(SENTINEL_NAME)!r}. Report: {observed!r}"
        ),
    )
