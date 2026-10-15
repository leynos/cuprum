"""Declarative contract for CI's nose duplication-detector provisioning.

The duplication gate runs in CI against a detector binary that CI downloads and
installs itself. That step is a supply-chain boundary — it fetches an installer
over the network, verifies a pinned digest, and then runs it — so its shape is
asserted here rather than left to review. The properties pinned are the ones
whose loss would be silent: that the installer is fetched over TLS, that its
digest is checked before extraction, that no strategy may fall back to a source
compile, and that the detector version CI installs is the version the gate
expects.

The step is a shell script inside ``ci.yml``, so these are structural
assertions over the parsed workflow rather than an execution of the script.
That is the same boundary ``test_mdtablefix_ci_installer`` draws for the
formatter installer: the YAML is the contract, and the script's behaviour is
reviewed rather than simulated.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.helpers.ci_runners import job_env, steps, workflow_env

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

_INSTALL_STEP = "Install nose duplication detector"
_GATE_JOB = "lint-test"
# The digest guards a specific archive name; asserting the pairing keeps a
# platform change from silently reusing a digest for the wrong artefact.
_ARCHIVE = "cargo-binstall-x86_64-unknown-linux-musl.tgz"
_PINNED_NOSE_VERSION = "0.20.0"


def _installer_step() -> Step:
    """Return the nose installer step from the job that runs the gate."""
    installers = [
        step for step in steps("ci.yml", _GATE_JOB) if step.get("name") == _INSTALL_STEP
    ]
    assert len(installers) == 1, (
        f"ci.yml:{_GATE_JOB} must declare exactly one {_INSTALL_STEP!r} step"
    )
    return installers[0]


def _installer_script() -> str:
    """Return the installer step's shell script."""
    script = _installer_step().get("run")
    assert isinstance(script, str), (
        "the nose installer must be a shell step whose run body is a string"
    )
    assert script.strip(), "the nose installer run script must not be blank"
    return script


def test_installer_is_fetched_over_tls() -> None:
    """The installer archive must be fetched over HTTPS with TLS enforced."""
    script = _installer_script()
    assert "curl" in script, "the installer must be fetched with curl"
    assert "--proto '=https'" in script, (
        "the fetch must pin the protocol to HTTPS, so a redirect cannot "
        "downgrade the transfer"
    )
    assert "--tlsv1.2" in script, "the fetch must require TLS 1.2 or better"
    assert (
        "https://github.com/cargo-bins/cargo-binstall/releases/download/" in script
    ), "the installer must come from the pinned release URL"


def test_digest_is_verified_before_extraction_and_execution() -> None:
    """The pinned SHA-256 must be checked before the archive is used."""
    script = _installer_script()
    assert "BINSTALL_SHA256" in script, "the digest must be a declared variable"
    assert "sha256sum --check" in script, (
        "the archive must be verified against its digest"
    )
    check_at = script.index("sha256sum --check")
    extract_at = script.index("tar -xzf")
    # The executable path, not the bare name: the release URL also contains
    # "cargo-binstall" and sits earlier in the script than the check.
    execute_at = script.index('"${binstall_directory}/cargo-binstall"')
    assert check_at < extract_at, (
        "the digest must be verified before the archive is extracted"
    )
    assert check_at < execute_at, (
        "the digest must be verified before the installer is executed"
    )


def test_archive_and_digest_are_pinned_together() -> None:
    """The digest must be checked against the fetched archive, not another file."""
    script = _installer_script()
    assert _ARCHIVE in script, "the installer archive name must be pinned"
    assert "BINSTALL_VERSION" in script, "the installer release must be pinned"
    digest_line = next(
        line for line in script.splitlines() if "sha256sum --check" in line
    )
    assert "BINSTALL_SHA256" in digest_line, (
        f"the digest check must name the pinned digest, got {digest_line.strip()!r}"
    )
    assert "binstall_archive" in digest_line, (
        "the digest check must name the archive it verifies, so the pin cannot "
        f"be reused for a different artefact, got {digest_line.strip()!r}"
    )


def test_no_source_compile_fallback_is_permitted() -> None:
    """A missing release asset must fail loudly, not start a source build."""
    script = _installer_script()
    assert "--disable-strategies compile,quick-install" in script, (
        "cargo-binstall must be forbidden from compiling nose from source; a "
        "missing asset should be a visible provisioning failure"
    )


def test_detector_version_matches_the_gate_expectation() -> None:
    """CI must install the same nose version the gate's settings pin."""
    script = _installer_script()
    assert '"nose-cli@${NOSE_VERSION}"' in script, (
        "the installer must take the version from the NOSE_VERSION variable"
    )
    assert workflow_env("ci.yml").get("NOSE_VERSION") == _PINNED_NOSE_VERSION, (
        f"ci.yml must pin NOSE_VERSION to {_PINNED_NOSE_VERSION}"
    )
    job_variables = job_env("ci.yml", _GATE_JOB)
    assert job_variables.get("NOSE_VERSION", _PINNED_NOSE_VERSION) == (
        _PINNED_NOSE_VERSION
    ), f"{_GATE_JOB} must not override NOSE_VERSION away from the pin"


@pytest.mark.parametrize(
    "constant_name",
    ["BINSTALL_VERSION", "BINSTALL_SHA256"],
)
def test_installer_pins_are_declared_in_the_step_environment(
    constant_name: str,
) -> None:
    """Each pin must be a step-scoped environment variable, not inline text.

    Step scope keeps the pins beside the script that uses them and out of the
    workflow-wide environment, so the digest is not visible to unrelated steps.
    """
    environment = _installer_step().get("env")
    assert isinstance(environment, dict), (
        "the nose installer must declare a step-scoped env mapping"
    )
    assert constant_name in environment, (
        f"the nose installer must declare {constant_name} in its step env"
    )
