"""Declarative contract for CI's shared mdtablefix installer."""

from __future__ import annotations

from tests.helpers.workflow import Workflow, step_named

_SHARED_ACTION_REVISION = "c5a54701c8603a0fa756a6b34c49bc2af75a6c11"
_INSTALL_MDTABLEFIX = (
    "leynos/shared-actions/.github/actions/install-mdtablefix@"
    f"{_SHARED_ACTION_REVISION}"
)


def test_mdtablefix_installer_uses_the_shared_prebuilt_action(
    workflow_data: Workflow,
) -> None:
    """CI delegates pinned prebuilt formatter installation to shared-actions."""
    step = step_named(workflow_data, "lint-test", "Install mdtablefix")

    assert step.get("uses") == _INSTALL_MDTABLEFIX, (
        "the Install mdtablefix CI step must use the pinned shared prebuilt "
        "installer action"
    )
    inputs = step.get("with")
    assert isinstance(inputs, dict), (
        "the Install mdtablefix CI step must pass inputs to the shared action"
    )
    assert inputs.get("version") == "${{ env.MDTABLEFIX_VERSION }}", (
        "the Install mdtablefix CI step must pass the workflow's pinned "
        "formatter version"
    )
    assert "run" not in step, (
        "the Install mdtablefix CI step must not retain a local source-build fallback"
    )
