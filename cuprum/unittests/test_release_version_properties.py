"""Property tests of the release's version check and pre-release classifier.

``check-version`` runs ``scripts/release_version.py``. Its pre-release answer
must agree with ``packaging``'s, an implementation written independently from
PEP 440, for versions spelt either way: SemVer (``1.2.3-beta.4``) and PEP 440
(``1.2.3b4``, ``rc``, ``a``, ``dev``), with post-release and local segments
that must not read as pre-releases. The step itself is run for a smaller
sample, since each example starts an interpreter.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from packaging.version import InvalidVersion, Version

from scripts.release_version import is_prerelease
from tests.helpers.release_workflow import run_version_check

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The release workflow's steps run under Bash on Linux.",
)

_RELEASE = st.lists(st.integers(0, 99), min_size=1, max_size=4).map(
    lambda parts: ".".join(map(str, parts))
)
_SEPARATOR = st.sampled_from(["", "-", ".", "_"])
_PRE = st.tuples(
    _SEPARATOR,
    st.sampled_from(["a", "alpha", "b", "beta", "c", "rc", "pre", "preview"]),
    st.sampled_from(["", "0", "4", ".4", "-4", "_12"]),
).map("".join)
_POST = st.sampled_from(["", ".post1", "-3", "post2", ".rev1", "r4"])
_DEV = st.sampled_from(["", ".dev0", "dev3", "-dev", ".dev"])
_LOCAL = st.sampled_from(["", "+local", "+rc1", "+dev.7", "+alpha", "+b2"])


def _is_valid(version: str) -> bool:
    """Return whether ``packaging`` accepts ``version``."""
    try:
        Version(version)
    except InvalidVersion:
        return False
    return True


_VERSIONS = (
    st
    .tuples(_RELEASE, st.one_of(st.just(""), _PRE), _POST, _DEV, _LOCAL)
    .map("".join)
    .flatmap(lambda version: st.sampled_from([version, version.upper()]))
    .filter(_is_valid)
)


@settings(max_examples=500)
@given(version=_VERSIONS)
def test_the_classifier_agrees_with_packaging(version: str) -> None:
    """A version is a pre-release exactly when ``packaging`` says it is."""
    assert is_prerelease(version) is Version(version).is_prerelease, (
        f"{version} must be classified as packaging classifies it"
    )


@settings(max_examples=25)
@given(version=_VERSIONS)
def test_the_step_reports_what_packaging_reports(version: str) -> None:
    """The checked-in step writes the oracle's answer for a matching tag."""
    with tempfile.TemporaryDirectory() as scratch:
        completed, written = run_version_check(Path(scratch), version, version)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    expected = str(Version(version).is_prerelease).lower()
    assert written == {"prerelease": expected}


@settings(max_examples=25)
@given(
    declared=_VERSIONS,
    tag=st.one_of(_VERSIONS, st.sampled_from(["", "v1.2.3", "1.2.3 ", "1.2.3\n"])),
    is_same=st.booleans(),
)
def test_the_step_accepts_exactly_the_declared_string(
    declared: str, tag: str, is_same: bool
) -> None:
    """The check passes if and only if the tag's version is the same string."""
    tag_version = declared if is_same else tag
    with tempfile.TemporaryDirectory() as scratch:
        completed, written = run_version_check(Path(scratch), declared, tag_version)

    accepted = completed.returncode == 0
    assert accepted is (tag_version == declared), (
        f"tag {tag_version!r} against {declared!r}: {completed.stdout}"
    )
    assert bool(written) is accepted, "only an accepted tag reports a status"
