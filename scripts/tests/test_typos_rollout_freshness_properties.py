"""Property-based tests for refresh degradation counters and freshness laws.

These complement the example-based refresh suites with Hypothesis properties:
degradation-counter state transitions over arbitrary reason sequences, and
the ETag-dominant remote-freshness decision over arbitrary validators.
"""

from __future__ import annotations

import collections
import dataclasses as dc
import datetime as dt
import email.utils
import typing as typ

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    import types

_shared_settings = settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


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
    min_value=dt.datetime(2020, 1, 1),  # ruff: ignore[call-datetime-without-tzinfo] - tz applied below
    max_value=dt.datetime(2030, 1, 1),  # ruff: ignore[call-datetime-without-tzinfo] - tz applied below
    timezones=st.just(dt.UTC),
).map(email.utils.format_datetime)


@dc.dataclass(frozen=True, slots=True)
class _EtagFreshnessCase:
    """Define validators for one ETag freshness decision."""

    saved_etag: str | None
    header_etag: str
    saved_date: str | None
    header_date: str | None


_etag_freshness_cases = st.builds(
    _EtagFreshnessCase,
    saved_etag=st.none() | _etags,
    header_etag=_etags,
    saved_date=st.none() | _http_dates,
    header_date=st.none() | _http_dates,
)


@given(case=_etag_freshness_cases)
@_shared_settings
def test_etag_alone_decides_freshness_when_present(
    refresh_module: types.ModuleType,
    case: _EtagFreshnessCase,
) -> None:
    """With an ETag in the response, dates never influence the decision."""
    saved: dict[str, object] = {}
    if case.saved_etag is not None:
        saved["etag"] = case.saved_etag
    if case.saved_date is not None:
        saved["last_modified"] = case.saved_date
    headers = {"ETag": case.header_etag}
    if case.header_date is not None:
        headers["Last-Modified"] = case.header_date

    assert refresh_module._remote_is_not_newer(saved, headers) is (
        case.header_etag == case.saved_etag
    ), (
        f"an ETag response ({case.header_etag!r} vs saved {case.saved_etag!r}) must be "
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
