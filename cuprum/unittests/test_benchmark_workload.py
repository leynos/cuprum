"""Unit tests for benchmark workload identity and protocol parsing.

`benchmarks/benchmark_workload.py` reads a plan back and states which workload
produced it, together with the protocol metadata the plan recorded. That
statement is load-bearing rather than cosmetic: the ratchet compares only
samples whose profile metadata agrees, and a maintainer reads the summary to
decide whether a ratio is real. Nothing downstream re-derives any of it from
the scenario matrix — the scenarios of one workload are indistinguishable in
shape from another's — so an error here is silent.

The tests below therefore pin the parsers directly rather than through a
report: every accepted workload, every documented rejection, and the
`payload_bytes` invariant that a plan with duplicate or unsorted scenario
sizes must still read back as one ascending distinct tuple. The property test
at the end asserts that invariant over generated scenario sequences, including
permutations of the same sequence, because a single curated example cannot
show that the result is independent of the order the plan happened to write.

Example
-------
pytest cuprum/unittests/test_benchmark_workload.py
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks.benchmark_workload import (
    CI_RATCHET_WORKLOAD,
    SMOKE_WORKLOAD,
    THROUGHPUT_SWEEP_WORKLOAD,
    WORKLOAD_PLAN_KEY,
    WORKLOADS,
    WorkloadProtocol,
    read_workload,
    read_workload_protocol,
)

#: The profile version a report renders, spelled here as a plan would carry it.
_PROFILE_VERSION = "profile-1"


def _protocol(**overrides: object) -> WorkloadProtocol:
    """Return a valid protocol with *overrides* applied to the defaults."""
    fields: dict[str, object] = {
        "workload": CI_RATCHET_WORKLOAD,
        "profile_version": _PROFILE_VERSION,
        "worker_iterations": 5,
        "payload_bytes": (1024,),
    }
    fields.update(overrides)
    return WorkloadProtocol(**typ.cast("dict[str, typ.Any]", fields))


class TestTheWorkloadIdentifier:
    """Which workload a plan declares, and what the reader makes of it."""

    def test_an_absent_workload_reads_as_the_throughput_sweep(self) -> None:
        """A plan predating the workload field measured the sweep.

        Plans written before the runner recorded a workload have no other
        candidate: the sweep was the only workload then available. Reading one
        as an error would make every archived artefact unreadable, so absence
        defaults rather than raises.
        """
        assert read_workload({}) == THROUGHPUT_SWEEP_WORKLOAD

    @pytest.mark.parametrize("workload", WORKLOADS)
    def test_every_declared_workload_round_trips(self, workload: str) -> None:
        """Each workload the runner can produce must read back as itself."""
        assert read_workload({WORKLOAD_PLAN_KEY: workload}) == workload

    def test_the_declared_workloads_are_the_ones_the_runner_selects(self) -> None:
        """The reader's accepted set must be the workloads a run can name.

        The tuple the reader validates against is the same tuple the config
        validates against, so a workload added to the runner without being
        added here would be selectable but unreadable — the plan would be
        written and then rejected on the way back in.
        """
        assert WORKLOADS == (
            THROUGHPUT_SWEEP_WORKLOAD,
            SMOKE_WORKLOAD,
            CI_RATCHET_WORKLOAD,
        ), "the accepted workloads must be exactly the ones the runner selects"

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(True, id="boolean"),
            pytest.param(7, id="integer"),
            pytest.param(1.5, id="float"),
            pytest.param(["smoke"], id="list"),
            pytest.param({"name": "smoke"}, id="mapping"),
        ],
    )
    def test_a_non_string_workload_is_rejected(self, value: object) -> None:
        """A present-but-non-string workload violates the type contract."""
        with pytest.raises(TypeError, match="workload must be a non-empty string"):
            read_workload({WORKLOAD_PLAN_KEY: value})

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace-only"),
            pytest.param("\t\n", id="whitespace-characters"),
        ],
    )
    def test_a_blank_workload_is_rejected(self, value: str) -> None:
        """An empty or whitespace-only workload names no workload at all."""
        with pytest.raises(ValueError, match="workload must be a non-empty string"):
            read_workload({WORKLOAD_PLAN_KEY: value})

    def test_an_unknown_workload_is_rejected(self) -> None:
        """A workload outside the runner's set cannot have produced the plan.

        The message names the expected set, because the reader's caller is a
        maintainer staring at an artefact whose workload no longer exists.
        """
        with pytest.raises(ValueError, match="unknown benchmark workload") as excinfo:
            read_workload({WORKLOAD_PLAN_KEY: "hyperfine-sweep"})

        message = str(excinfo.value)
        assert "unknown benchmark workload 'hyperfine-sweep'" in message
        for workload in WORKLOADS:
            assert workload in message, (
                f"the rejection must name the accepted workload {workload!r}; "
                f"found: {message}"
            )

    def test_an_explicit_null_workload_reads_as_the_sweep(self) -> None:
        """A JSON `null` is absence, not a rejected type.

        A plan that spells the key with a null value is making the same
        statement as one that omits it — nothing recorded a workload — so it
        takes the same default rather than failing a type check.
        """
        assert read_workload({WORKLOAD_PLAN_KEY: None}) == THROUGHPUT_SWEEP_WORKLOAD


class TestTheProtocolValueObject:
    """That a constructed protocol describes a measurement a run could make."""

    def test_a_valid_protocol_round_trips_its_fields(self) -> None:
        """Construction must preserve exactly what it was given."""
        protocol = _protocol(payload_bytes=(1024, 4096))

        assert protocol.workload == CI_RATCHET_WORKLOAD
        assert protocol.profile_version == _PROFILE_VERSION
        assert protocol.worker_iterations == 5
        assert protocol.payload_bytes == (1024, 4096)

    def test_an_unknown_workload_cannot_be_constructed(self) -> None:
        """The value object rejects a workload no formatter could describe.

        This is what makes the report's description lookup total: an unknown
        workload fails here, at construction, rather than at render time in a
        query that reads as pure.
        """
        with pytest.raises(ValueError, match="unknown benchmark workload"):
            _protocol(workload="hyperfine-sweep")

    @pytest.mark.parametrize(
        "workload",
        [
            pytest.param(True, id="boolean"),
            pytest.param(7, id="integer"),
            pytest.param(None, id="none"),
        ],
    )
    def test_a_non_string_workload_cannot_be_constructed(
        self,
        workload: object,
    ) -> None:
        """A non-string workload is a type error, not a lookup failure."""
        with pytest.raises(TypeError, match="workload must be a non-empty string"):
            _protocol(workload=workload)

    @pytest.mark.parametrize(
        "profile_version",
        [
            pytest.param("", id="empty"),
            pytest.param("  \t", id="whitespace-only"),
        ],
    )
    def test_a_blank_profile_version_cannot_be_constructed(
        self,
        profile_version: str,
    ) -> None:
        """A blank profile names no profile, so it must not masquerade as one."""
        with pytest.raises(ValueError, match="profile_version must be a non-empty"):
            _protocol(profile_version=profile_version)

    @pytest.mark.parametrize(
        "profile_version",
        [
            pytest.param(3, id="integer"),
            pytest.param(["profile-1"], id="list"),
        ],
    )
    def test_a_non_string_profile_version_cannot_be_constructed(
        self,
        profile_version: object,
    ) -> None:
        """A non-string profile violates the type contract."""
        with pytest.raises(TypeError, match="profile_version must be a non-empty"):
            _protocol(profile_version=profile_version)

    @pytest.mark.parametrize(
        ("payload_bytes", "kind", "expected"),
        [
            pytest.param(
                [1024],
                TypeError,
                "payload_bytes must be a tuple",
                id="list",
            ),
            pytest.param(
                {1024},
                TypeError,
                "payload_bytes must be a tuple",
                id="set",
            ),
            pytest.param(
                "1024",
                TypeError,
                "payload_bytes must be a tuple",
                id="string",
            ),
            pytest.param(
                (True,),
                TypeError,
                "payload_bytes must contain only ints",
                id="boolean-entry",
            ),
            pytest.param(
                (1024.0,),
                TypeError,
                "payload_bytes must contain only ints",
                id="float-entry",
            ),
            pytest.param(
                (4096, 1024),
                ValueError,
                "payload_bytes must be distinct and ascending",
                id="unsorted",
            ),
            pytest.param(
                (1024, 1024),
                ValueError,
                "payload_bytes must be distinct and ascending",
                id="duplicate",
            ),
        ],
    )
    def test_payload_bytes_are_validated(
        self,
        payload_bytes: object,
        kind: type[Exception],
        expected: str,
    ) -> None:
        """Payload sizes must be the tuple a plan reads back as.

        A protocol describes what a plan recorded, and a plan's sizes read
        back ascending and distinct. Accepting a set, a float, or a repeated
        size would let a report describe a measurement shape no run produces.
        """
        with pytest.raises(kind, match=expected):
            _protocol(payload_bytes=payload_bytes)

    def test_an_empty_payload_list_is_accepted(self) -> None:
        """A plan with no scenarios records no sizes, which is not an error."""
        protocol = _protocol(payload_bytes=())

        assert protocol.payload_bytes == ()

    def test_optional_fields_accept_absence(self) -> None:
        """A plan that omits optional metadata is valid, not defaulted."""
        protocol = _protocol(profile_version=None, worker_iterations=None)

        assert protocol.profile_version is None
        assert protocol.worker_iterations is None


class TestReadingAProtocol:
    """What a plan's recorded metadata reads back as."""

    def test_a_plan_without_optional_metadata_reads_as_absent(self) -> None:
        """Absence stays absent; the reader must not invent a protocol."""
        protocol = read_workload_protocol({})

        assert protocol.workload == THROUGHPUT_SWEEP_WORKLOAD
        assert protocol.profile_version is None
        assert protocol.worker_iterations is None
        assert protocol.payload_bytes == ()

    def test_a_complete_plan_reads_back_its_protocol(self) -> None:
        """Every recorded field must survive the read."""
        protocol = read_workload_protocol({
            WORKLOAD_PLAN_KEY: CI_RATCHET_WORKLOAD,
            "benchmark_profile_version": _PROFILE_VERSION,
            "worker_iterations": 5,
            "scenarios": [{"payload_bytes": 4096}, {"payload_bytes": 1024}],
        })

        assert protocol.workload == CI_RATCHET_WORKLOAD
        assert protocol.profile_version == _PROFILE_VERSION
        assert protocol.worker_iterations == 5
        assert protocol.payload_bytes == (1024, 4096)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("", id="empty"),
            pytest.param(" \t", id="whitespace-only"),
        ],
    )
    def test_a_blank_profile_in_a_plan_is_rejected(self, value: str) -> None:
        """A blank profile version names no profile, so it cannot be read."""
        with pytest.raises(
            ValueError,
            match="benchmark_profile_version must be a non-empty string",
        ):
            read_workload_protocol({"benchmark_profile_version": value})

    def test_a_non_string_profile_in_a_plan_is_rejected(self) -> None:
        """A profile version must be a string."""
        with pytest.raises(
            TypeError,
            match="benchmark_profile_version must be a non-empty string",
        ):
            read_workload_protocol({"benchmark_profile_version": 3})

    @pytest.mark.parametrize(
        ("value", "kind", "expected"),
        [
            pytest.param(
                True,
                TypeError,
                "worker_iterations must be an int",
                id="bool",
            ),
            pytest.param(
                5.0,
                TypeError,
                "worker_iterations must be an int",
                id="float",
            ),
            pytest.param(
                "5",
                TypeError,
                "worker_iterations must be an int",
                id="string",
            ),
            pytest.param(
                0,
                ValueError,
                r"worker_iterations must be >= 1",
                id="zero",
            ),
            pytest.param(
                -3,
                ValueError,
                r"worker_iterations must be >= 1",
                id="negative",
            ),
        ],
    )
    def test_an_invalid_worker_iteration_count_is_rejected(
        self,
        value: object,
        kind: type[Exception],
        expected: str,
    ) -> None:
        """The count must be a real positive integer.

        A `bool` is an `int` in Python, so `True` would otherwise read as one
        worker iteration — a protocol the plan never recorded. Zero and
        negative counts describe no measurement at all.
        """
        with pytest.raises(kind, match=expected):
            read_workload_protocol({"worker_iterations": value})

    @pytest.mark.parametrize(
        "scenarios",
        [
            pytest.param("scenario", id="string"),
            pytest.param(b"scenario", id="bytes"),
            pytest.param(7, id="integer"),
            pytest.param({"0": {"payload_bytes": 1024}}, id="mapping"),
        ],
    )
    def test_a_non_sequence_scenario_container_is_rejected(
        self,
        scenarios: object,
    ) -> None:
        """Scenarios are a sequence, and strings are not one here.

        A string is a sequence of characters, so it would otherwise iterate
        into per-character entries and fail later with a message about
        `scenarios[0]` that names nothing a reader could act on.
        """
        with pytest.raises(TypeError, match="scenarios must be a sequence"):
            read_workload_protocol({"scenarios": scenarios})

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param("python-ratchet-single-nocb", id="string"),
            pytest.param(7, id="integer"),
            pytest.param(None, id="none"),
        ],
    )
    def test_a_non_mapping_scenario_entry_is_rejected(self, entry: object) -> None:
        """Each scenario must be an object; the message names its position."""
        with pytest.raises(TypeError, match=r"scenarios\[1\] must be an object"):
            read_workload_protocol({
                "scenarios": [{"payload_bytes": 1024}, entry],
            })

    @pytest.mark.parametrize(
        ("size", "kind", "expected"),
        [
            pytest.param(
                True,
                TypeError,
                r"scenarios\[0\]\.payload_bytes must be an int",
                id="boolean",
            ),
            pytest.param(
                1024.0,
                TypeError,
                r"scenarios\[0\]\.payload_bytes must be an int",
                id="float",
            ),
            pytest.param(
                "1024",
                TypeError,
                r"scenarios\[0\]\.payload_bytes must be an int",
                id="string",
            ),
        ],
    )
    def test_an_invalid_scenario_payload_size_is_rejected(
        self,
        size: object,
        kind: type[Exception],
        expected: str,
    ) -> None:
        """A declared size must be a real integer, and be named by position."""
        with pytest.raises(kind, match=expected):
            read_workload_protocol({"scenarios": [{"payload_bytes": size}]})

    @pytest.mark.parametrize(
        "scenario",
        [
            pytest.param({}, id="empty-object"),
            pytest.param({"name": "python-ratchet-single-nocb"}, id="named-only"),
            pytest.param({"payload_bytes": None}, id="explicit-null"),
        ],
    )
    def test_a_scenario_without_a_payload_size_is_skipped(
        self,
        scenario: dict[str, object],
    ) -> None:
        """A scenario may legitimately declare no size, so absence is not an error."""
        protocol = read_workload_protocol({
            "scenarios": [scenario, {"payload_bytes": 1024}],
        })

        assert protocol.payload_bytes == (1024,)

    def test_duplicate_and_unsorted_payloads_read_back_distinct_and_ascending(
        self,
    ) -> None:
        """A plan's sizes are a set for comparison purposes, written ascending.

        The matrix may name the same size for several scenarios — the CI
        ratchet does, once per backend and depth — and write them in whatever
        order it built them. A consumer comparing two protocols must see one
        value per distinct size, in a fixed order, or two runs that measured
        the same payloads would look like different protocols.
        """
        protocol = read_workload_protocol({
            "scenarios": [
                {"payload_bytes": 4096},
                {"payload_bytes": 1024},
                {"payload_bytes": 4096},
                {"payload_bytes": 1024},
                {"payload_bytes": 2048},
            ],
        })

        assert protocol.payload_bytes == (1024, 2048, 4096)

    def test_the_protocol_reader_rejects_an_unknown_workload(self) -> None:
        """A protocol read must carry a workload the formatter can describe."""
        with pytest.raises(ValueError, match="unknown benchmark workload"):
            read_workload_protocol({WORKLOAD_PLAN_KEY: "hyperfine-sweep"})


@st.composite
def _scenario_sequences(draw: st.DrawFn) -> list[dict[str, object]]:
    """Generate scenarios with duplicated, omitted, and reordered sizes."""
    sizes = draw(
        st.lists(
            st.integers(min_value=0, max_value=1 << 20),
            max_size=8,
        )
    )
    declared: list[int | None] = draw(
        st.lists(
            st.none() | st.sampled_from(sizes) if sizes else st.none(),
            min_size=len(sizes),
            max_size=len(sizes),
        )
    )
    scenarios: list[dict[str, object]] = [
        {} if size is None else {"payload_bytes": size} for size in declared
    ]
    draw(st.randoms()).shuffle(scenarios)
    return scenarios


@given(scenarios=_scenario_sequences())
def test_payload_sizes_are_distinct_ascending_and_permutation_invariant(
    scenarios: list[dict[str, object]],
) -> None:
    """`payload_bytes` is the distinct ascending sizes the scenarios declare.

    The property is stated against the generated input rather than against the
    reader's own helper: the expected value is recomputed here, so the test
    cannot pass by sharing a misconception with the implementation. Permutation
    invariance is asserted over the same sequence read in reverse, which is the
    strongest statement available without regenerating an unrelated sequence.
    """
    declared = [
        size
        for scenario in scenarios
        if isinstance(size := scenario.get("payload_bytes"), int)
        and not isinstance(size, bool)
    ]
    expected = tuple(sorted(set(declared)))

    protocol = read_workload_protocol({"scenarios": scenarios})

    assert protocol.payload_bytes == expected, (
        "the parsed payload sizes must be the distinct ascending sizes the "
        f"scenarios declared; scenarios={scenarios!r}"
    )
    reordered = read_workload_protocol({"scenarios": list(reversed(scenarios))})
    assert reordered == protocol, (
        "reading the scenarios in a different order must describe the same "
        f"protocol; scenarios={scenarios!r}"
    )
