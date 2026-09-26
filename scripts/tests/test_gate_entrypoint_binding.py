"""The gate's entry point must adjudicate the tree it was launched from.

``scripts`` is a namespace package, so CPython builds its search path from
every ``sys.path`` entry holding a ``scripts`` directory. A development
install of the application contributes the checkout root, which meant a gate
started from a copied workspace — the arrangement the blocking tests and the
adoption smoke test both use — still imported the application's own
``scripts.nose_detector``. It then read the application's ``pyproject.toml``,
its allow list, and its detector binary, and printed a clean result for a tree
it had never looked at. The defect was found by running the gate from a
workspace outside the checkout, which no test did.

The regression this guards against is invisible to a test that only checks the
gate *runs*: both the right tree and the wrong tree produce output. So the
probe plants a duplicate the workspace declares no exception for and asserts
the gate fails. Reading the checkout's manifest instead makes it pass.

``PYTHONPATH`` is deliberately left unset here, unlike every other gate test.
Setting it to the workspace root puts that root ahead of the development
install's ``.pth`` entry, so the correct tree wins on path order alone and the
binding is never exercised — the probe would then pass with or without it.
Dropping ``PYTHONPATH`` is what a developer gets by running the script
directly, and that is the arrangement where the development install's ``.pth``
entry decides which ``scripts`` a namespace lookup finds.
"""

from __future__ import annotations

import textwrap
import typing as typ

import pytest

from scripts.tests.duplication_gate_test_support import (
    REPOSITORY_ROOT,
    copied_gate_workspace,
    detector,
    gate_environment,
    run_gate_command,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

_BODY = textwrap.dedent(
    """\
    def {name}(items):
        total = 0.0
        for item in items:
            price = item["price"] * item["quantity"]
            if item.get("taxable"):
                price *= 1.2
            if item.get("discount"):
                price -= item["discount"]
            total += price
        if total < 0:
            total = 0.0
        return round(total, 2)
    """
)


def _planted_workspace(tmp_path: Path) -> Path:
    """Build a workspace holding one planted duplicate and no exceptions."""
    workspace = copied_gate_workspace(tmp_path)
    package = workspace / "cuprum"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "alpha.py").write_text(_BODY.format(name="total_for"), encoding="utf-8")
    (package / "beta.py").write_text(_BODY.format(name="sum_for"), encoding="utf-8")
    (workspace / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "binding-probe"
            version = "0"

            [tool.nose]
            version = "0.20.0"
            roots = ["cuprum"]
            mode = "syntax,semantic,near"
            min-size = 24
            surface = "all"
            top = 30

            [tool.duplication_gate]
            """
        ),
        encoding="utf-8",
    )
    return workspace


def _skip_without_the_detector(error: detector.GateExecutionError) -> typ.NoReturn:
    """Skip the probe when the pinned detector is not provisioned.

    ``NoReturn`` is load-bearing rather than decorative, for the reason
    ``test_duplication_gate_blocking`` records: without it a
    ``return``-in-``try`` beside a ``skip``-in-``except`` reads as a function
    that can also fall off the end of the ``except`` branch, which is an
    inconsistent return.
    """
    pytest.skip(str(error))


def _pinned_binary() -> str:
    """Return the pinned detector, skipping when it is not provisioned."""
    settings = detector.load_settings(REPOSITORY_ROOT / "pyproject.toml")
    try:
        return detector.resolve_binary(settings)
    except detector.GateExecutionError as error:  # pragma: no cover
        _skip_without_the_detector(error)


class TestEntrypointBinding:
    """The entry point reads the workspace's configuration, not the checkout's."""

    def test_the_workspace_manifest_decides_the_outcome(self, tmp_path: Path) -> None:
        """A copied gate blocks on its own tree when run as a file path.

        The workspace declares `min-size = 24` and no allow entries, so the
        planted pair must fail the gate with an unsuppressed family. The
        application checkout covers every family it reports, so a gate that
        resolved ``scripts`` through the ambient namespace path returns zero
        and a pass line instead.

        The "no exceptions" half is asserted rather than compared against a
        literal count. A count taken from the checkout would go stale the
        moment this repository's own adjudication changed, and a stale literal
        cannot fail: it silently stops guarding anything. The absence of any
        allowance is the property that actually distinguishes the two trees.
        """
        workspace = _planted_workspace(tmp_path)
        environment = {
            key: value
            for key, value in gate_environment(
                workspace, NOSE_BIN=_pinned_binary()
            ).items()
            if key != "PYTHONPATH"
        }
        result = run_gate_command(
            workspace,
            "check",
            environment=environment,
        )

        combined = f"{result.stdout}{result.stderr}"
        assert result.returncode == 1, (
            "A workspace whose duplicate no exception covers must fail the "
            "gate; a pass here means the checkout's manifest decided the "
            f"outcome.\n{combined}"
        )
        assert "duplication gate passed" not in combined, (
            "The checkout's passing verdict must not describe a workspace "
            "that reports an unsuppressed family."
        )
        assert "allowed by reasoned exceptions" not in combined, (
            "The checkout's allow entries must not reach a workspace report."
        )
        assert "cuprum/alpha.py" in combined, (
            "The report must name the workspace's first planted copy."
        )
        assert "cuprum/beta.py" in combined, (
            "The report must name the workspace's second planted copy."
        )
