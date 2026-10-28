"""Guard the release workflow's default failure gates."""

from tests.helpers.ci_workflows import job
from tests.helpers.release_workflow import WORKFLOW, step


def test_pypi_staging_failure_stops_the_job() -> None:
    """A failed staging step must stop the PyPI publisher."""
    staging = step("publish-pypi", "Skip artefacts already on PyPI")

    assert "continue-on-error" not in staging, (
        "failed PyPI staging must stop the publisher"
    )


def test_release_publication_requires_successful_dependencies() -> None:
    """The release job must retain GitHub's default needs-success gate."""
    release = job(WORKFLOW, "publish-release")

    assert "if" not in release, (
        "publish-release must require its dependencies to succeed"
    )
