"""Property-based tests for dictionary merge/mapping and refresh policy laws.

These complement the example-based suites with Hypothesis properties over
arbitrary inputs: merge algebra (commutativity, associativity, idempotence,
and union semantics), stem-expansion invariants of the generated word
mappings, degradation-counter state transitions, and the ETag-dominant
remote-freshness decision.
"""

from __future__ import annotations

import collections
import dataclasses as dc
import datetime as dt
import email.utils
import typing as typ

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    import types

# Disjoint alphabets keep the generated corpora collision-free by
# construction: stem expansions always contain ``is``/``iz``, so words drawn
# from these alphabets can never alias an expanded stem, an accepted word,
# or a correction key from another pool.
_STEM_ALPHABET = "abcde"
_ACCEPTED_ALPHABET = "mno"
_CORRECTION_ALPHABET = "qwx"

_stems = st.lists(
    st.text(alphabet=_STEM_ALPHABET, min_size=1, max_size=5),
    max_size=5,
)
_accepted = st.lists(
    st.text(alphabet=_ACCEPTED_ALPHABET, min_size=1, max_size=5),
    max_size=5,
)
_patterns = st.lists(
    st.text(alphabet="prs", min_size=1, max_size=5),
    max_size=4,
)
_corrections_pool = st.dictionaries(
    st.text(alphabet=_CORRECTION_ALPHABET, min_size=1, max_size=5),
    st.text(alphabet=_STEM_ALPHABET, min_size=1, max_size=5),
    max_size=5,
)

_shared_settings = settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@dc.dataclass(frozen=True, slots=True)
class _MergeUnionCase:
    """Define generated inputs for one dictionary-union merge.

    Attributes
    ----------
    pool : dict[str, str]
        Correction pool split across both sides, so shared keys agree.
    stems_a : list[str]
        Oxford stems for the left-hand dictionary.
    stems_b : list[str]
        Oxford stems for the right-hand dictionary.
    accepted_a : list[str]
        Accepted words for the left-hand dictionary.
    accepted_b : list[str]
        Accepted words for the right-hand dictionary.
    patterns_a : list[str]
        Ignore patterns for the left-hand dictionary.
    patterns_b : list[str]
        Ignore patterns for the right-hand dictionary.
    """

    pool: dict[str, str]
    stems_a: list[str]
    stems_b: list[str]
    accepted_a: list[str]
    accepted_b: list[str]
    patterns_a: list[str]
    patterns_b: list[str]


_merge_union_cases = st.builds(
    _MergeUnionCase,
    pool=_corrections_pool,
    stems_a=_stems,
    stems_b=_stems,
    accepted_a=_accepted,
    accepted_b=_accepted,
    patterns_a=_patterns,
    patterns_b=_patterns,
)


def _dictionary(
    rollout: types.ModuleType,
    *,
    stems: list[str] | None = None,
    accepted: list[str] | None = None,
    corrections: dict[str, str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> object:
    """Build a ``Dictionary`` from unnormalized generated parts."""
    return rollout.Dictionary(
        stems=tuple(stems or ()),
        accepted=tuple(accepted or ()),
        corrections=tuple((corrections or {}).items()),
        ignore_patterns=tuple(ignore_patterns or ()),
    )


@given(pool=_corrections_pool, stems_a=_stems, stems_b=_stems)
@_shared_settings
def test_merge_is_commutative_without_conflicts(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    pool: dict[str, str],
    stems_a: list[str],
    stems_b: list[str],
) -> None:
    """Merging non-conflicting dictionaries is order-independent."""
    _, rollout, _ = rollout_modules
    # Overlapping keys are drawn from one pool, so shared keys always agree.
    items = sorted(pool.items())
    left = _dictionary(rollout, stems=stems_a, corrections=dict(items[::2]))
    right = _dictionary(rollout, stems=stems_b, corrections=dict(items[1::2]))

    assert rollout.merge_dictionaries(left, right) == rollout.merge_dictionaries(
        right, left
    ), f"merge must be commutative for non-conflicting inputs {left!r} and {right!r}"


@given(pool=_corrections_pool, stems=st.lists(_stems, min_size=3, max_size=3))
@_shared_settings
def test_merge_is_associative_without_conflicts(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    pool: dict[str, str],
    stems: list[list[str]],
) -> None:
    """Merging non-conflicting dictionaries can be grouped either way."""
    _, rollout, _ = rollout_modules
    items = sorted(pool.items())
    parts = [
        _dictionary(rollout, stems=stems[offset], corrections=dict(items[offset::3]))
        for offset in range(3)
    ]

    left_first = rollout.merge_dictionaries(
        rollout.merge_dictionaries(parts[0], parts[1]), parts[2]
    )
    right_first = rollout.merge_dictionaries(
        parts[0], rollout.merge_dictionaries(parts[1], parts[2])
    )

    assert left_first == right_first, (
        f"merge must be associative for non-conflicting parts {parts!r}"
    )


@given(case=_merge_union_cases)
@_shared_settings
def test_merge_unions_fields_and_is_idempotent(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    case: _MergeUnionCase,
) -> None:
    """A merge is the sorted union of both sides and absorbs re-merging."""
    _, rollout, _ = rollout_modules
    items = sorted(case.pool.items())
    left = _dictionary(
        rollout,
        stems=case.stems_a,
        accepted=case.accepted_a,
        corrections=dict(items[::2]),
        ignore_patterns=case.patterns_a,
    )
    right = _dictionary(
        rollout,
        stems=case.stems_b,
        accepted=case.accepted_b,
        corrections=dict(items[1::2]),
        ignore_patterns=case.patterns_b,
    )

    merged = rollout.merge_dictionaries(left, right)

    assert merged.stems == tuple(sorted(set(case.stems_a) | set(case.stems_b))), (
        "merged stems must be the sorted union of both sides"
    )
    assert merged.accepted == tuple(
        sorted(set(case.accepted_a) | set(case.accepted_b))
    ), "merged accepted words must be the sorted union of both sides"
    assert merged.corrections == tuple(sorted(case.pool.items())), (
        "merged corrections must be the sorted union of both sides"
    )
    assert merged.ignore_patterns == tuple(
        sorted(set(case.patterns_a) | set(case.patterns_b))
    ), "merged ignore patterns must be the sorted union of both sides"
    assert rollout.merge_dictionaries(merged, merged) == merged, (
        "merging a normalized dictionary with itself must be a fixed point"
    )
    assert rollout.merge_dictionaries(merged, right) == merged, (
        "re-merging an already absorbed overlay must not change the result"
    )


@given(
    stems=_stems,
    accepted=_accepted,
    corrections=_corrections_pool,
)
@_shared_settings
def test_generated_mappings_expand_every_stem_and_word(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    stems: list[str],
    accepted: list[str],
    corrections: dict[str, str],
) -> None:
    """Word mappings cover stems, accepted words, and corrections exactly."""
    _, rollout, _ = rollout_modules
    dictionary = _dictionary(
        rollout,
        stems=stems,
        accepted=accepted,
        corrections=corrections,
    )

    mappings = rollout.generate_word_mappings(dictionary)

    expected_keys: set[str] = set(accepted) | set(corrections)
    for stem in stems:
        for plain_british, oxford in rollout.SUFFIX_PAIRS:
            expected_keys |= {f"{stem}{plain_british}", f"{stem}{oxford}"}
            assert mappings[f"{stem}{plain_british}"] == f"{stem}{oxford}", (
                f"the plain-British expansion of {stem!r} must map to its Oxford form"
            )
            assert mappings[f"{stem}{oxford}"] == f"{stem}{oxford}", (
                f"the Oxford expansion of {stem!r} must map to itself"
            )
    for word in accepted:
        assert mappings[word] == word, "an accepted word must map to itself"
    for word, correction in corrections.items():
        assert mappings[word] == correction, (
            "an explicit correction must survive stem expansion"
        )
    assert set(mappings) == expected_keys, (
        "mappings must contain exactly the expanded and explicit words"
    )
    assert list(mappings) == sorted(mappings), (
        "generated mappings must be deterministically sorted"
    )


@given(
    word=st.text(alphabet=_CORRECTION_ALPHABET, min_size=1, max_size=5),
    first=st.text(alphabet=_STEM_ALPHABET, min_size=1, max_size=5),
    second=st.text(alphabet=_STEM_ALPHABET, min_size=1, max_size=5),
)
@_shared_settings
def test_merge_rejects_any_conflicting_correction(
    rollout_modules: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    word: str,
    first: str,
    second: str,
) -> None:
    """Every conflicting correction pair is rejected, whatever the words."""
    _, rollout, _ = rollout_modules
    assume(first != second)
    base = _dictionary(rollout, corrections={word: first})
    local = _dictionary(rollout, corrections={word: second})

    with pytest.raises(ValueError, match="conflicting correction"):
        rollout.merge_dictionaries(base, local)


@given(
    reasons=st.lists(
        st.sampled_from([
            "https_redirect_downgrade",
            "stale_cache",
            "offline_cache",
            "unknown_reason",
        ]),
        max_size=20,
    )
)
@_shared_settings
def test_degradation_counters_count_only_known_reasons(
    refresh_module: types.ModuleType,
    reasons: list[str],
) -> None:
    """Counter state equals the tally of known reasons; unknowns are ignored."""
    refresh_module.reset_degradations()
    for reason in reasons:
        refresh_module._record_degradation(reason)

    tally = collections.Counter(reasons)
    snapshot = refresh_module.degradation_snapshot()

    assert set(snapshot) == {
        "https_redirect_downgrade",
        "stale_cache",
        "offline_cache",
    }, "unknown reasons must never create new counter entries"
    assert all(snapshot[reason] == tally[reason] for reason in snapshot), (
        f"counters {snapshot!r} must match the recorded tally {tally!r}"
    )

    refresh_module.reset_degradations()
    assert all(
        count == 0 for count in refresh_module.degradation_snapshot().values()
    ), "reset must return every counter to zero"


_etags = st.text(alphabet="ab", min_size=1, max_size=3).map(lambda tag: f'"{tag}"')
_http_dates = st.datetimes(
    min_value=dt.datetime(2020, 1, 1),  # noqa: DTZ001 - tz applied below
    max_value=dt.datetime(2030, 1, 1),  # noqa: DTZ001 - tz applied below
    timezones=st.just(dt.UTC),
).map(email.utils.format_datetime)


@given(
    saved_etag=st.none() | _etags,
    header_etag=_etags,
    saved_date=st.none() | _http_dates,
    header_date=st.none() | _http_dates,
)
@_shared_settings
def test_etag_alone_decides_freshness_when_present(
    refresh_module: types.ModuleType,
    saved_etag: str | None,
    header_etag: str,
    saved_date: str | None,
    header_date: str | None,
) -> None:
    """With an ETag in the response, dates never influence the decision."""
    saved: dict[str, object] = {}
    if saved_etag is not None:
        saved["etag"] = saved_etag
    if saved_date is not None:
        saved["last_modified"] = saved_date
    headers = {"ETag": header_etag}
    if header_date is not None:
        headers["Last-Modified"] = header_date

    assert refresh_module._remote_is_not_newer(saved, headers) is (
        header_etag == saved_etag
    ), (
        f"an ETag response ({header_etag!r} vs saved {saved_etag!r}) must be "
        "decided by ETag equality alone"
    )


@given(saved_date=_http_dates, header_date=_http_dates)
@_shared_settings
def test_dates_decide_freshness_without_an_etag(
    refresh_module: types.ModuleType,
    saved_date: str,
    header_date: str,
) -> None:
    """Without an ETag, well-formed dates order the freshness decision."""
    result = refresh_module._remote_is_not_newer(
        {"last_modified": saved_date},
        {"Last-Modified": header_date},
    )

    expected = email.utils.parsedate_to_datetime(
        header_date
    ) <= email.utils.parsedate_to_datetime(saved_date)
    assert result is expected, (
        f"freshness for {header_date!r} against saved {saved_date!r} must "
        "follow the parsed date ordering"
    )
