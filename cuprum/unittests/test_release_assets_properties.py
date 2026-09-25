"""Property and model-based tests of the release's asset reconciliation.

``scripts/release_assets.py`` decides, for every artefact name, where its
canonical bytes come from and which destination lacks it. These properties
hold for arbitrary name sets and destination holdings, and the state machine
replays arbitrary sequences of release runs, including runs that stop part-way
through an upload, against model PyPI and GitHub stores that refuse to
overwrite anything.
"""

from __future__ import annotations

import hashlib
import tempfile
import typing as typ
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from scripts.release_assets import (
    PypiFile,
    Source,
    State,
    mismatches,
    plan,
    stage_github,
    stage_pypi,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_POOL = (
    "c-1-cp312-abi3-linux.whl",
    "c-1-cp312-abi3-macos.whl",
    "c-1-cp312-abi3-win.whl",
    "c-1-py3-none-any.whl",
    "c-1.tar.gz",
)


def _digest(content: bytes) -> str:
    """Return the SHA-256 hex digest of ``content``."""
    return hashlib.sha256(content).hexdigest()


@st.composite
def _holdings(draw: st.DrawFn) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Draw this run's names and the names each destination already holds."""
    local = draw(st.frozensets(st.sampled_from(_POOL), min_size=1))
    # Destinations may also hold names this run did not build.
    on_pypi = draw(st.frozensets(st.sampled_from(_POOL)))
    on_github = draw(st.frozensets(st.sampled_from(_POOL)))
    return local, on_pypi, on_github


@given(holdings=_holdings())
def test_each_destination_is_sent_exactly_what_it_lacks(
    holdings: tuple[frozenset[str], frozenset[str], frozenset[str]],
) -> None:
    """Uploads are this run's names minus what the destination holds."""
    local, on_pypi, on_github = holdings

    decided = plan(local, on_pypi, on_github)

    assert decided.pypi_uploads == local - on_pypi
    assert decided.github_uploads == local - on_github
    assert not decided.pypi_uploads & on_pypi, "nothing on PyPI is re-uploaded"
    assert not decided.github_uploads & on_github, "no asset is re-uploaded"


@given(holdings=_holdings())
def test_the_canonical_source_prefers_pypi_then_github(
    holdings: tuple[frozenset[str], frozenset[str], frozenset[str]],
) -> None:
    """PyPI's bytes win, then GitHub's, and this run's only when neither has one."""
    local, on_pypi, on_github = holdings

    canonical = plan(local, on_pypi, on_github).canonical

    assert set(canonical) == local
    for name, source in canonical.items():
        expected = (
            Source.PYPI
            if name in on_pypi
            else Source.GITHUB
            if name in on_github
            else Source.LOCAL
        )
        assert source is expected, f"{name} must come from {expected}"


def _stage(
    root: Path,
    local: cabc.Iterable[str],
    pypi: dict[str, bytes],
    github: dict[str, bytes],
) -> State:
    """Write this run's files and GitHub's carried bytes; return the state."""
    publish, carried = root / "publish", root / "carried"
    publish.mkdir()
    carried.mkdir()
    for name in local:
        (publish / name).write_bytes(f"local {name}".encode())
    for name, content in github.items():
        (carried / name).write_bytes(content)
    return State(
        publish,
        {
            name: PypiFile(_digest(b), f"https://f.test/{name}")
            for name, b in pypi.items()
        },
        {name: _digest(content) for name, content in github.items()},
    )


@given(holdings=_holdings())
def test_staging_for_pypi_leaves_exactly_the_canonical_missing_bytes(
    holdings: tuple[frozenset[str], frozenset[str], frozenset[str]],
) -> None:
    """The files left to upload are PyPI's gaps, GitHub's bytes winning."""
    local, on_pypi, on_github = holdings
    github = {name: f"github {name}".encode() for name in on_github}
    with tempfile.TemporaryDirectory() as scratch:
        state = _stage(Path(scratch), local, dict.fromkeys(on_pypi, b"pypi"), github)

        remaining = stage_pypi(state, Path(scratch) / "carried")

        left = {path.name: path.read_bytes() for path in state.publish.iterdir()}
    assert set(left) == local - on_pypi
    assert remaining is bool(left), "the upload guard must match what is left"
    for name, content in left.items():
        expected = github.get(name, f"local {name}".encode())
        assert content == expected, f"{name} must carry its canonical bytes"


@given(holdings=_holdings())
def test_staging_for_github_takes_pypi_bytes_before_this_runs(
    holdings: tuple[frozenset[str], frozenset[str], frozenset[str]],
) -> None:
    """Names on PyPI are fetched from it; only the rest use this run's files."""
    local, on_pypi, on_github = holdings
    github = dict.fromkeys(on_github, b"github")
    with tempfile.TemporaryDirectory() as scratch:
        state = _stage(Path(scratch), local, dict.fromkeys(on_pypi, b"pypi"), github)
        bundles = Path(scratch) / "bundles"
        bundles.mkdir()

        fetch = stage_github(state, bundles, Path(scratch) / "upload")

        copied = {path.name for path in (Path(scratch) / "upload").iterdir()}
    lacking = local - on_github
    assert {name for name, _ in fetch} == lacking & on_pypi
    assert copied == lacking - on_pypi, "only names PyPI lacks use local bytes"


#: The points at which a modelled release run can stop.
_STOPS = ("complete", "after-draft", "during-pypi", "during-github")


class ReleaseRuns(RuleBasedStateMachine):
    """Replay release runs against model destinations that never overwrite."""

    def __init__(self) -> None:
        """Start with nothing published anywhere."""
        super().__init__()
        self.pypi: dict[str, bytes] = {}
        self.github: dict[str, bytes] = {}
        self.runs = 0

    @staticmethod
    def _upload(destination: dict[str, bytes], name: str, content: bytes) -> None:
        """Publish one file, failing the model if it would overwrite."""
        assert name not in destination, f"{name} would be overwritten"
        destination[name] = content

    @rule(name=st.sampled_from(_POOL))
    def an_earlier_workflow_attached_a_new_name_to_github(self, name: str) -> None:
        """Attach a new name to GitHub first, as the retired workflow could."""
        if name not in self.pypi and name not in self.github:
            self.github[name] = f"earlier {name}".encode()

    @rule(stop=st.sampled_from(_STOPS), budget=st.integers(0, len(_POOL)))
    def a_release_run(self, stop: str, budget: int) -> None:
        """Rebuild every name, then publish as the workflow does, maybe stopping."""
        self.runs += 1
        local = {name: f"run {self.runs} {name}".encode() for name in _POOL}
        snapshot = dict(self.github)
        if stop == "after-draft":
            return
        to_pypi = plan(local, self.pypi, snapshot)
        pending = sorted(to_pypi.pypi_uploads)
        for name in pending[: budget if stop == "during-pypi" else None]:
            carried = to_pypi.canonical[name] is Source.GITHUB
            self._upload(self.pypi, name, snapshot[name] if carried else local[name])
        if stop == "during-pypi":
            return
        to_github = plan(local, self.pypi, self.github)
        pending = sorted(to_github.github_uploads)
        for name in pending[: budget if stop == "during-github" else None]:
            from_pypi = to_github.canonical[name] is Source.PYPI
            self._upload(
                self.github, name, self.pypi[name] if from_pypi else local[name]
            )
        if stop == "complete":
            self._assert_converged(local)

    def _assert_converged(self, local: cabc.Mapping[str, bytes]) -> None:
        """Require the digest check a complete run ends with to pass."""
        problems = mismatches(
            local,
            {name: _digest(content) for name, content in self.pypi.items()},
            {name: _digest(content) for name, content in self.github.items()},
        )
        assert problems == [], problems

    @invariant()
    def names_on_both_sides_hold_the_same_bytes(self) -> None:
        """Even a run that stopped part-way leaves no disagreeing name."""
        for name in self.pypi.keys() & self.github.keys():
            assert self.pypi[name] == self.github[name], f"{name} differs"


TestReleaseRuns = ReleaseRuns.TestCase
TestReleaseRuns.settings = settings(max_examples=100, stateful_step_count=12)
