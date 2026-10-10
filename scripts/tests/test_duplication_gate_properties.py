"""Property tests for duplication-gate matching and normalization invariants.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_duplication_gate_properties.py``), the merged revision
of PR #276, under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from scripts.tests.duplication_gate_test_support import allowlist, detector, gate

_PATHS = st.sampled_from((
    "cuprum/a.py",
    "cuprum/b.py",
    "cuprum/c.py",
    "cuprum/d.py",
))
_NAMES = st.sampled_from(("alpha", "beta", None))


@st.composite
def _locations(draw: st.DrawFn) -> detector.Location:
    """Build one reported location with a stable span."""
    start = draw(st.integers(min_value=1, max_value=200))
    return detector.Location(
        file=draw(_PATHS),
        start=start,
        end=start + draw(st.integers(min_value=0, max_value=40)),
        name=draw(_NAMES),
    )


@st.composite
def _findings(draw: st.DrawFn) -> detector.Finding:
    """Build one duplication family over two or more locations."""
    return detector.Finding(
        witness=draw(st.sampled_from(("exact", "copy-paste", "similar"))),
        value=float(draw(st.integers(min_value=0, max_value=100))),
        locations=tuple(draw(st.lists(_locations(), min_size=2, max_size=5))),
    )


@st.composite
def _allow_entries(draw: st.DrawFn) -> allowlist.AllowEntry:
    """Build one valid unit or members allow entry."""
    keys = draw(
        st.one_of(
            st.tuples(_PATHS),
            st.lists(_PATHS, min_size=2, max_size=3, unique=True).map(tuple),
        )
    )
    return allowlist.AllowEntry(keys=keys, reason="property")


@given(finding=_findings(), entry=_allow_entries())
def test_entries_allow_only_fully_covered_families(
    finding: detector.Finding,
    entry: allowlist.AllowEntry,
) -> None:
    """An entry silences a family exactly when it covers every location."""
    covered = all(
        any(allowlist.key_matches(key, location) for key in entry.keys)
        for location in finding.locations
    )
    assert entry.matches(finding) is covered, (
        "Matching must require coverage of every reported location."
    )


@given(finding=_findings())
def test_file_keys_cover_their_own_locations(finding: detector.Finding) -> None:
    """Listing every file in a family always silences it."""
    entry = allowlist.AllowEntry(
        keys=tuple({location.file for location in finding.locations}),
        reason="property",
    )
    assert entry.matches(finding), (
        "An entry naming every participating file must silence the family."
    )


@given(
    findings=st.lists(_findings(), max_size=8),
    allowlist=st.lists(_allow_entries(), max_size=6),
)
def test_partition_conserves_findings_and_identifies_stale_entries(
    findings: list[detector.Finding],
    allowlist: list[allowlist.AllowEntry],
) -> None:
    """Every finding is exactly blocking or allowed and stale entries match none."""
    blocking, allowed, stale = gate.partition_findings(findings, allowlist)

    assert len(blocking) + len(allowed) == len(findings), (
        "Partitioning must retain every reported finding exactly once."
    )
    assert all(
        any(entry.matches(finding) for entry in allowlist) for finding in allowed
    ), "Allowed findings must have a matching allow entry."
    assert all(
        not any(entry.matches(finding) for entry in allowlist) for finding in blocking
    ), "Blocking findings must have no matching allow entry."
    # Loop variables are deliberately the other way round from the reference:
    # a stale entry is an AllowEntry, and it must match none of the findings.
    assert all(
        not any(entry.matches(finding) for finding in findings) for entry in stale
    ), "Stale entries must not match any current finding."


@given(findings=st.lists(_findings(), min_size=1, max_size=8))
def test_normalization_orders_values_then_locations(
    findings: list[detector.Finding],
) -> None:
    """Normalized findings use the documented deterministic ordering."""
    report = {
        "families": [
            {
                "witness": finding.witness,
                "value": finding.value,
                "locations": [
                    {
                        "file": location.file,
                        "start": location.start,
                        "end": location.end,
                        "name": location.name,
                    }
                    for location in finding.locations
                ],
            }
            for finding in findings
        ]
    }
    normalized = detector.normalize_findings(report)
    assert len(normalized) == len(findings), (
        "Normalization must conserve the finding count."
    )
    sort_keys = [(-finding.value, finding.label) for finding in normalized]
    assert sort_keys == sorted(sort_keys), (
        "Normalization must sort by descending value then source location."
    )
