"""Structural contracts of the release workflow and of every ``run`` script.

These read the parsed workflow rather than executing it: which job holds which
token scope, what gates the upload, and which values may reach a shell. The
executed behaviour of the same steps lives in ``test_release_publish_steps.py``
and ``test_release_github_steps.py``.
"""

from __future__ import annotations

import re
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.helpers.ci_workflows import ROOT, jobs, workflow_document
from tests.helpers.release_workflow import WORKFLOW, step, step_script
from tests.helpers.strict_yaml import load

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_PINNED = re.compile(r"@[0-9a-f]{40}$")
_CARGO_CHECK = "leynos/shared-actions/.github/actions/ensure-cargo-version"
_PYPROJECT_CHECK = "Check the pyproject.toml version against the tag"
_GH_VERSION_CHECK = "Check the GitHub CLI version"
_GH_VERSION_COMMAND = "gh --version | python3 scripts/release_gh_version.py"

#: Each job's exact token scopes. Only the GitHub Release jobs may write
#: repository contents, and only the attesting and uploading jobs may mint an
#: OpenID Connect token. `publish-pypi` reads contents only to check out
#: `scripts/`; it sees the draft through `draft-release`'s snapshot instead.
_EXPECTED_PERMISSIONS: typ.Final = {
    "check-version": {"contents": "read"},
    "build-wheels": {"contents": "read"},
    "attest": {"contents": "read", "id-token": "write", "attestations": "write"},
    "publish-pypi": {"contents": "read", "id-token": "write"},
    "draft-release": {"contents": "write", "attestations": "read"},
    "publish-release": {"contents": "write", "attestations": "read"},
}
_FETCH_INDEX = "Fetch the PyPI index"
_PROVENANCE_STEP = "Verify provenance of PyPI bytes"
_PROVENANCE_DIR = "pypi-provenance"

#: The `id` of the publisher step that reports whether the CodeScene token
#: exists, and that step's whole command.
CREDENTIAL_CHECK_ID: typ.Final = "codescene-token"
CREDENTIAL_CHECK_COMMAND: typ.Final = (
    'echo "available=${{ secrets.CS_ACCESS_TOKEN != \'\' }}" >> "$GITHUB_OUTPUT"'
)

#: The one reviewed template expansion inside a shell script. It renders only
#: `true` or `false`, and the shared CV-005 contract (`token.check-step`)
#: requires the secret to be read there rather than through an `env` value.
_ALLOWED_EXPANSIONS: typ.Final = frozenset({
    ("coverage-main.yml", CREDENTIAL_CHECK_ID, CREDENTIAL_CHECK_COMMAND)
})


def _job(name: str) -> dict[str, typ.Any]:
    """Return one release job."""
    return typ.cast("dict[str, typ.Any]", jobs(WORKFLOW)[name])


def _needs(name: str) -> set[str]:
    """Return every job ``name`` waits on, directly or transitively."""
    declared = _job(name).get("needs", [])
    direct = {declared} if isinstance(declared, str) else set(declared)
    return direct.union(*(_needs(parent) for parent in direct))


def _uses(job_name: str, prefix: str) -> dict[str, typ.Any]:
    """Return the single step of ``job_name`` that uses action ``prefix``."""
    found = [
        item
        for item in _job(job_name)["steps"]
        if str(item.get("uses", "")).startswith(f"{prefix}@")
    ]
    assert len(found) == 1, f"{job_name} must use {prefix} exactly once"
    assert _PINNED.search(found[0]["uses"]), f"{prefix} must be pinned by commit SHA"
    return found[0]


def test_every_job_holds_only_the_scopes_it_needs() -> None:
    """Nothing inherits a write scope from the top level."""
    assert workflow_document(WORKFLOW).get("permissions") == {}, (
        "release.yml must grant nothing at the top level"
    )
    declared = {name: _job(name).get("permissions") for name in jobs(WORKFLOW)}
    assert declared == _EXPECTED_PERMISSIONS


def test_checkouts_do_not_persist_the_token() -> None:
    """A checked-out repository must not keep a usable token on disk."""
    checkouts = [
        item
        for job in jobs(WORKFLOW).values()
        for item in typ.cast("dict[str, typ.Any]", job).get("steps", [])
        if str(item.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts, "the version check must check the repository out"
    assert all(item["with"]["persist-credentials"] is False for item in checkouts)


def test_the_pypi_upload_runs_in_the_pypi_environment() -> None:
    """Trusted Publishing is bound to the protected ``pypi`` environment."""
    assert _job("publish-pypi")["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/project/cuprum/",
    }


def test_a_repushed_tag_queues_instead_of_cancelling() -> None:
    """A cancelled release could stop part-way through the upload."""
    concurrency = workflow_document(WORKFLOW)["concurrency"]
    assert concurrency == {
        "group": "release-${{ github.ref }}",
        "cancel-in-progress": False,
    }


def test_the_index_lookup_retries_and_is_bounded() -> None:
    """Both index readers share one bounded, retrying, timed-out lookup."""
    script = step_script("publish-pypi", _FETCH_INDEX)
    assert step_script("publish-release", _FETCH_INDEX) == script
    for fragment in (
        'while [ "${attempt}" -lt 6 ]; do',
        "--connect-timeout 10",
        "--max-time 60",
        "000|429|5??)",
        "attempts=%s",
    ):
        assert fragment in script, f"the index lookup must include {fragment!r}"
    assert "404) echo '{\"files\": []}' > pypi-index.json ;;" in script, (
        "a project absent from PyPI must still read as an empty index"
    )


def test_every_publishing_job_waits_on_the_version_check() -> None:
    """No attestation, upload, or release happens for a mismatched tag."""
    assert _needs("publish-pypi") >= {"attest", "draft-release"}, (
        "PyPI needs the draft's snapshot to carry GitHub's bytes over"
    )
    assert "publish-pypi" in _needs("publish-release"), (
        "GitHub's missing assets take PyPI's bytes, so PyPI goes first"
    )
    for name in ("attest", "publish-pypi", "draft-release", "publish-release"):
        assert "check-version" in _needs(name), f"{name} must wait on check-version"
    cargo = _uses("check-version", _CARGO_CHECK)
    assert cargo["with"] == {"manifests": "rust/cuprum-rust/Cargo.toml"}
    pyproject = step("check-version", _PYPROJECT_CHECK)
    assert pyproject["env"] == {
        "TAG_VERSION": "${{ steps.cargo-version.outputs.version }}"
    }, "the pyproject check must compare against the same tag-derived version"


def test_the_cli_version_is_checked_before_anything_is_published() -> None:
    """An old CLI must stop the release before PyPI holds any of its files."""
    found = step("check-version", _GH_VERSION_CHECK)
    assert "if" not in found, (
        "the early gate must run on every release, including one with no carryover"
    )
    assert step_script("check-version", _GH_VERSION_CHECK) == _GH_VERSION_COMMAND, (
        "check-version must use the same version gate as the verifier steps"
    )
    assert _GH_VERSION_COMMAND in step_script("publish-release", _PROVENANCE_STEP), (
        "the verifier step must still check the CLI that verifies attestations"
    )
    telemetry = next(
        item
        for item in _job("check-version")["steps"]
        if item.get("uses") == "./.github/actions/release-telemetry"
    )
    assert (
        "version_mismatch=${{ steps.gh-version.outcome }}" in telemetry["with"]["steps"]
    ), "check-version telemetry must record the CLI version outcome"


def test_the_published_artefacts_are_the_attested_ones() -> None:
    """Provenance covers every file, and later jobs ship exactly those bytes."""
    attest = _uses("attest", "actions/attest-build-provenance")
    assert attest["with"] == {"subject-path": "dist/publish/*"}
    for name in ("publish-pypi", "draft-release", "publish-release"):
        assert "attest" in _needs(name), f"{name} must wait on the attestation"
        downloads = [
            item["with"]
            for item in _job(name)["steps"]
            if str(item.get("uses", "")).startswith("actions/download-artifact@")
        ]
        assert {"name": "release-dist", "path": "dist"} in downloads, (
            f"{name} must take the attested artefact, not rebuilt files"
        )


def test_carried_assets_are_verified_before_they_reach_the_pypi_job() -> None:
    """The draft snapshot hands policy-verified records to the token-free job."""
    draft_steps = _job("draft-release")["steps"]
    names = [item.get("name") for item in draft_steps]
    handoff_index = next(
        index for index, item in enumerate(draft_steps) if item.get("id") == "handoff"
    )
    assert (
        names.index("Snapshot the release assets")
        < names.index("Verify carried asset provenance")
        < handoff_index
    ), "verification must precede handoff"

    verifier = step_script("draft-release", "Verify carried asset provenance")
    for fragment in (
        "carryover_file=github-state/carryover.txt",
        'if [ ! -f "${carryover_file}" ]; then',
        'if [ ! -s "${carryover_file}" ]; then',
        'echo "verification_failures=0" >> "${GITHUB_OUTPUT}"',
        "required_version=2.102.0",
        "sort -V",
        '--repo "${GH_REPO}"',
        '--signer-workflow "${GH_REPO}/.github/workflows/release.yml"',
        '--source-ref "${GITHUB_REF}"',
        "--deny-self-hosted-runners",
        "--format json",
        'rm -f "${record}"',
        "verification_failures=$((verification_failures + 1))",
        '>> "${GITHUB_OUTPUT}"',
        "::warning title=attestation::",
    ):
        assert fragment in verifier, f"the verifier must enforce {fragment!r}"
    assert verifier.index('if [ ! -s "${carryover_file}" ]; then') < verifier.index(
        "required_version=2.102.0"
    ), "the CLI minimum applies only when carried assets need verification"
    assert "${{" not in verifier, (
        "the verifier script must not interpolate workflow expressions"
    )

    pending = step_script("publish-pypi", "Skip artefacts already on PyPI")
    assert "--provenance github-state/provenance" in pending, (
        "PyPI staging must receive the verifier records"
    )
    telemetry = next(
        item
        for item in draft_steps
        if item.get("uses") == "./.github/actions/release-telemetry"
    )
    assert (
        "attestation=${{ steps.attestation.outcome }}" in telemetry["with"]["steps"]
    ), "draft-release telemetry must record the attestation outcome"
    assert telemetry["with"]["verification-failures"] == (
        "${{ steps.attestation.outputs.verification_failures }}"
    ), "draft-release telemetry must retain the bounded verifier failure count"
    pypi_steps = _job("publish-pypi")["steps"]
    pypi_telemetry = next(
        item
        for item in pypi_steps
        if item.get("uses") == "./.github/actions/release-telemetry"
    )
    assert (
        "digest_mismatch=${{ steps.pending.outcome }}"
        in pypi_telemetry["with"]["steps"]
    ), "publish-pypi telemetry must record the staging outcome"


def test_the_upload_keeps_pypi_attestations_and_is_idempotent() -> None:
    """PEP 740 attestations stay on, and a partial earlier upload is tolerated."""
    upload = _uses("publish-pypi", "pypa/gh-action-pypi-publish")
    assert upload["if"] == "steps.pending.outputs.remaining == 'true'"
    assert upload["with"] == {"packages-dir": "dist/publish/", "skip-existing": True}


def test_the_release_is_published_only_after_pypi() -> None:
    """A failed upload or a digest mismatch leaves the GitHub Release a draft."""
    assert {"draft-release", "publish-pypi"} <= _needs("publish-release")
    assert "publish-pypi" in _job("publish-release")["needs"], (
        "publish-release must wait directly on successful PyPI publication"
    )
    assert "--draft" in step_script(
        "draft-release", "Create or reuse the draft release"
    )
    names = [item.get("name") for item in _job("publish-release")["steps"]]
    fetched = names.index("Check the fetched PyPI bytes")
    provenance = names.index(_PROVENANCE_STEP)
    upload = names.index("Upload the assets the release lacks")
    check = names.index("Check both destinations hold the same bytes")
    assert (
        fetched
        < provenance
        < upload
        < check
        < names.index("Publish the GitHub Release")
    ), (
        "provenance must precede the upload, and the digests must be compared "
        "after the upload and before the release"
    )
    assert "--draft=false" in step_script(
        "publish-release", "Publish the GitHub Release"
    )


def test_pypi_bytes_are_verified_before_the_release_upload() -> None:
    """Fetched PyPI bytes need approved provenance, not just the index digest."""
    found = step("publish-release", _PROVENANCE_STEP)
    assert "if" not in found, (
        "the gate must run unconditionally, even with an empty fetch list"
    )
    script = step_script("publish-release", _PROVENANCE_STEP)
    for fragment in (
        "gh --version | python3 scripts/release_gh_version.py",
        f"mkdir -p {_PROVENANCE_DIR}",
        '--repo "${GITHUB_REPOSITORY}"',
        '--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/release.yml"',
        '--source-ref "${GITHUB_REF}"',
        '--source-digest "${GITHUB_SHA}"',
        "--predicate-type https://slsa.dev/provenance/v1",
        "--deny-self-hosted-runners",
        "--format json",
        f'record="{_PROVENANCE_DIR}/${{name}}.json"',
        'rm -f "${record}"',
        "failed=0",
        "failed=1",
        '[ "${failed}" -eq 0 ] || exit 1',
        "::error title=pypi-provenance::",
        "check-provenance --publish dist/publish",
        "--index pypi-index.json",
        "--output github-upload",
        f"--provenance {_PROVENANCE_DIR}",
    ):
        assert fragment in script, f"the provenance gate must include {fragment!r}"
    assert "${{" not in script, (
        "the provenance script must not interpolate workflow expressions"
    )
    assert "done < pypi-fetch.tsv" in script, (
        "only the files actually fetched from PyPI need verification"
    )
    loop = script.partition("done < pypi-fetch.tsv")[0]
    assert "exit 1" not in loop, (
        "a failed verification must not stop the remaining checks"
    )
    assert script.index("done < pypi-fetch.tsv") < script.index(
        '[ "${failed}" -eq 0 ] || exit 1'
    ), "the gate must finish verifying every file before it exits"
    telemetry = next(
        item
        for item in _job("publish-release")["steps"]
        if item.get("uses") == "./.github/actions/release-telemetry"
    )
    recorded = telemetry["with"]["steps"]
    attestation = "attestation=${{ steps.pypi-provenance.outcome }}"
    assert attestation in recorded, "provenance failures must not count as setup"


def test_nothing_is_ever_overwritten_and_pypi_holds_no_github_token() -> None:
    """No script clobbers an asset, and the PyPI job cannot touch GitHub."""
    scripts = [holder["run"] for _, holder in _run_scripts(jobs(WORKFLOW), WORKFLOW)]
    assert scripts, "the scan must reach the release scripts"
    assert not any("--clobber" in script for script in scripts)
    assert "GH_TOKEN" not in str(_job("publish-pypi")), (
        "the job holding the PyPI token must not also hold a GitHub token"
    )


def test_every_scripts_checkout_is_sparse_and_credential_free() -> None:
    """The jobs that run `scripts/` fetch it and the telemetry action only."""
    for name in ("attest", "draft-release", "publish-pypi", "publish-release"):
        checkout = _uses(name, "actions/checkout")
        assert checkout["with"] == {
            "persist-credentials": False,
            "sparse-checkout": "scripts\n.github/actions/release-telemetry\n",
        }, f"{name} must check out only what its steps run"


def _run_scripts(
    document: object, where: str
) -> cabc.Iterator[tuple[str, dict[str, typ.Any]]]:
    """Yield every mapping that declares a ``run`` script, with its location."""
    if isinstance(document, dict):
        if isinstance(document.get("run"), str):
            yield where, document
        for key, value in document.items():
            yield from _run_scripts(value, f"{where}.{key}")
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from _run_scripts(value, f"{where}[{index}]")


def _expansions_in(name: str, document: object) -> list[str]:
    """Return each ``run`` script location in ``document`` that expands ``${{``."""
    return [
        where
        for where, holder in _run_scripts(document, name)
        if "${{" in holder["run"]
        and (name, holder.get("id"), holder["run"].strip()) not in _ALLOWED_EXPANSIONS
    ]


def _document_name(path: pth.Path) -> str:
    """Name a workflow by file name and a composite action by its path."""
    if path.parent.name == "workflows":
        return path.name
    return path.relative_to(ROOT).as_posix()


def _ci_documents() -> dict[str, object]:
    """Parse every workflow and composite action in the repository."""
    paths = [
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
        *sorted((ROOT / ".github" / "actions").glob("*/action.yml")),
    ]
    return {
        _document_name(path): load(path.read_text(encoding="utf-8"), path.name)
        for path in paths
    }


def test_no_run_script_expands_a_template_expression() -> None:
    """Values reach a shell through ``env``, never by pasting into the script."""
    documents = _ci_documents()
    scanned = sum(
        len(list(_run_scripts(document, name))) for name, document in documents.items()
    )
    assert scanned > 20, "the scan must reach the repository's run scripts"
    assert ".github/actions/build-wheels/action.yml" in documents
    findings = [
        where
        for name, document in documents.items()
        for where in _expansions_in(name, document)
    ]
    assert not findings, f"route these values through env: {findings}"


@pytest.mark.parametrize(
    ("name", "document", "is_flagged"),
    [
        ("x.yml", {"steps": [{"run": 'echo "${{ inputs.target }}"'}]}, True),
        ("x.yml", {"runs": {"steps": [{"run": "make ${{ matrix.check }}"}]}}, True),
        (
            "x.yml",
            {"steps": [{"env": {"T": "${{ inputs.t }}"}, "run": 'echo "$T"'}]},
            False,
        ),
        (
            "x.yml",
            {"steps": [{"id": CREDENTIAL_CHECK_ID, "run": CREDENTIAL_CHECK_COMMAND}]},
            True,
        ),
        (
            "coverage-main.yml",
            {"steps": [{"id": CREDENTIAL_CHECK_ID, "run": CREDENTIAL_CHECK_COMMAND}]},
            False,
        ),
    ],
    ids=[
        "input",
        "composite-matrix",
        "through-env",
        "exception-elsewhere",
        "exception",
    ],
)
def test_the_expansion_scan_discriminates(
    name: str, document: object, *, is_flagged: bool
) -> None:
    """The scan flags pasted values and admits only the one reviewed exception."""
    assert bool(_expansions_in(name, document)) is is_flagged


_LEAVES = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=4))
#: `run` often enough that holders, and non-string `run` values, both appear.
_KEYS = st.sampled_from(["run", "run", "steps", "with", "id"])
_DOCUMENTS = st.recursive(
    _LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(_KEYS, children, max_size=4)
    ),
    max_leaves=30,
)


def _run_holders(document: object) -> list[int]:
    """Return the identity of every mapping with a string ``run``, iteratively."""
    found: list[int] = []
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if isinstance(node.get("run"), str):
                found.append(id(node))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return found


@given(document=_DOCUMENTS)
def test_the_run_script_scan_finds_exactly_the_run_holders(document: object) -> None:
    """The scan yields every mapping with a string ``run`` and nothing else."""
    scanned = list(_run_scripts(document, "doc"))

    assert sorted(id(holder) for _, holder in scanned) == sorted(
        _run_holders(document)
    ), "a missed holder is a script the expansion scan never reads"
    locations = [where for where, _ in scanned]
    assert len(set(locations)) == len(locations), "each holder has one location"
