"""Hold `make test` to the colour environment it pins for every pytest session.

`make test` runs the Python suite as a sequence of pytest sessions and stops at
the first failing one. CPython 3.14 and later colour `argparse` help when the
environment asks for it, so a test that compares captured help text with plain
prose fails under `FORCE_COLOR=1`, and that one failure hides every later
session. The gate therefore removes the force variables and exports a no-colour
policy before each session.

The recipes are driven rather than read, because a prefix that no recipe calls
is a policy that never applies. `tests/helpers/pytest_colour_gate.py` holds the
stubbed-`uv` machinery that records each session and its ordinary child; this
module holds the assertions. One case overrides `PYTEST` on the command line:
the prefix deliberately lives outside that variable, and a caller override must
not be able to drop it. See "Colour environment for the test gate" in
`docs/developers-guide.md`.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.pytest_colour_gate import (
    GATE_TIMEOUT_SECONDS,
    assert_policy,
    drive_gate,
    gate_skip_reason,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

#: The bootstrap session's own module, run by `test-selection`.
_SELECTION_MODULE = "tests/test_ci_test_selection_contract.py"

_SKIP_REASON = gate_skip_reason()

pytestmark = pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON or "drivable")


@pytest.mark.timeout(GATE_TIMEOUT_SECONDS + 30)
@pytest.mark.parametrize(
    "override_pytest",
    [
        pytest.param(False, id="makefile-pytest"),
        pytest.param(True, id="pytest-override"),
    ],
)
def test_every_python_gate_session_pins_colour(
    tmp_path: Path, override_pytest: bool
) -> None:
    """Each session, and its children, see one no-colour policy.

    The expected call sequence is exact rather than a set: the bootstrap
    selector check runs first, then one individual invocation, then one grouped
    `--dist loadgroup` invocation. A prefix that reached only some of them, or
    a recipe that stopped running one, changes the sequence and fails here.
    """
    records, selectors = drive_gate(tmp_path, override_pytest=override_pytest)
    expected = [
        [_SELECTION_MODULE],
        [
            "-v",
            "-n",
            "0",
            "--deselect",
            str(selectors.grouped),
            str(selectors.individual),
        ],
        ["-v", "-n", "0", "--dist", "loadgroup", str(selectors.grouped)],
    ]
    assert [record["argv"] for record in records] == expected, (
        "the gate must run the bootstrap, individual, and grouped sessions in "
        f"that order; recorded: {[record['argv'] for record in records]!r}"
    )
    for index, record in enumerate(records):
        assert_policy(record["environment"], subject=f"session {index} environment")
        assert_policy(record["child"], subject=f"session {index} child environment")
