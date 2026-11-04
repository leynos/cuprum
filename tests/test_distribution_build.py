"""Check the packaging frontend's command contract and watchdog configuration."""

from __future__ import annotations

import typing as typ
from types import SimpleNamespace

import pytest

from cuprum import ProcessGroupPolicy
from tests.helpers.distribution_build import (
    BUILD_TIMEOUT_ENVIRONMENT_VARIABLE,
    BuildFailure,
    BuildRequest,
    SubprocessFrontend,
    configured_build_timeout_seconds,
    configured_build_timeouts,
    distribution_arguments,
    distribution_test_timeout_seconds,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import ExecutionContext

type _Backend = typ.Literal["uv", "maturin"]
type _Format = typ.Literal["wheel", "sdist"]

#: Cold Maturin wheel build measured on a loaded developer host on 2026-10-30.
_MEASURED_COLD_MATURIN_SECONDS = 163
_DEFAULT_BUILD_TIMEOUTS: dict[_Backend, int] = {"uv": 180, "maturin": 3600}
_ARCHIVE_SUFFIXES: dict[_Format, str] = {
    "wheel": "cuprum.whl",
    "sdist": "cuprum.tar.gz",
}


class _RecordingRunner:
    """Capture each command and its context, returning a controlled result."""

    def __init__(
        self, *, exit_code: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        """Store the result the frontend will observe."""
        self.calls: list[tuple[tuple[str, ...], ExecutionContext]] = []
        self._result = SimpleNamespace(
            exit_code=exit_code, stdout=stdout, stderr=stderr
        )

    def __call__(
        self, arguments: tuple[str, ...], context: ExecutionContext
    ) -> SimpleNamespace:
        """Record the invocation and return the configured result."""
        self.calls.append((arguments, context))
        return self._result


class _StaticFileSystem:
    """Return a fixed archive scan without touching the real filesystem."""

    def __init__(self, archives: list[Path]) -> None:
        """Store the archives the scan will report."""
        self._archives = archives

    def prepare(self, directory: Path) -> None:
        """Accept directory commands without creating anything."""

    def archives(self, directory: Path, pattern: str) -> list[Path]:
        """Return the stored archives."""
        return list(self._archives)

    def append_line(self, path: Path, line: str) -> None:
        """Accept timing appends without writing anything."""


def _request(
    tmp_path: Path, backend: _Backend, archive_format: _Format
) -> BuildRequest:
    """Create one build request with an offline run-local environment."""
    return BuildRequest(
        backend=backend,
        archive_format=archive_format,
        repository_root=tmp_path,
        destination=tmp_path / "artefacts" / backend / archive_format,
        environment={
            "CARGO_HOME": str(tmp_path / "cargo-home"),
            "CARGO_TARGET_DIR": str(tmp_path / "target"),
            "CARGO_NET_OFFLINE": "true",
        },
    )


def _frontend(
    runner: _RecordingRunner,
    archives: list[Path],
    *,
    build_timeouts: dict[_Backend, int] | None = None,
) -> SubprocessFrontend:
    """Create a frontend over the recording runner and static archive scan."""
    return SubprocessFrontend(
        runner=runner,
        build_timeouts=build_timeouts or _DEFAULT_BUILD_TIMEOUTS,
        filesystem=_StaticFileSystem(archives),
    )


@pytest.mark.parametrize(
    ("configured_value", "backend", "expected_seconds"),
    [
        (None, "uv", 180),
        (None, "maturin", 3600),
        ("300", "uv", 300),
        ("300", "maturin", 3600),
        ("600", "maturin", 3600),
        ("7200", "uv", 7200),
        ("7200", "maturin", 7200),
    ],
)
def test_packaging_timeout_override_can_only_raise_the_backend_default(
    configured_value: str | None,
    backend: _Backend,
    expected_seconds: int,
) -> None:
    """The override adds headroom and never lowers a backend bound."""
    assert (
        configured_build_timeout_seconds(configured_value, backend=backend)
        == expected_seconds
    ), "the packaging command must use the larger of override and backend default"


@pytest.mark.parametrize("configured_value", ["0", "-1", "invalid"])
def test_packaging_timeout_rejects_invalid_override(configured_value: str) -> None:
    """Invalid frontend limits fail clearly instead of disabling the watchdog."""
    with pytest.raises(ValueError, match="CUPRUM_PACKAGING_BUILD_TIMEOUT"):
        configured_build_timeout_seconds(configured_value, backend="uv")


def test_configured_timeouts_read_the_supplied_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the mapping a caller supplies decides the watchdogs."""
    monkeypatch.setenv(BUILD_TIMEOUT_ENVIRONMENT_VARIABLE, "7200")

    assertion_message = (
        "the factory must not consult the ambient process environment for its "
        "watchdog configuration"
    )
    assert configured_build_timeouts({}) == _DEFAULT_BUILD_TIMEOUTS, assertion_message
    assert configured_build_timeouts({BUILD_TIMEOUT_ENVIRONMENT_VARIABLE: "900"}) == {
        "uv": 900,
        "maturin": 3600,
    }, "an explicit mapping must supply the override"


def test_packaging_test_timeout_covers_frontend_limit_with_margin() -> None:
    """Consumer tests leave 60 seconds beyond the largest frontend limit."""
    largest_frontend_limit = max(_DEFAULT_BUILD_TIMEOUTS.values())
    assert distribution_test_timeout_seconds({}) - largest_frontend_limit == 60, (
        "the consumer timeout must stay 60 seconds above the largest build cap"
    )


def test_maturin_budget_keeps_headroom_over_its_measured_cold_build() -> None:
    """The native watchdog leaves room for a cold build and admission waits."""
    maturin_limit = configured_build_timeouts({})["maturin"]
    assert maturin_limit > configured_build_timeouts({})["uv"], (
        "the native build must keep a larger bound than the Python-only backend"
    )
    assert maturin_limit >= 3 * _MEASURED_COLD_MATURIN_SECONDS, (
        "the Maturin watchdog must retain headroom for a cold target and the "
        "compiler-admission waits no workload lease can exclude"
    )


@pytest.mark.parametrize(
    ("backend", "archive_format", "expected_prefix"),
    [
        ("uv", "wheel", ("build", "--wheel", "--out-dir")),
        ("uv", "sdist", ("build", "--sdist", "--out-dir")),
        (
            "maturin",
            "wheel",
            (
                "run",
                "maturin",
                "build",
                "--manifest-path",
                "rust/cuprum-rust/Cargo.toml",
                "--out",
            ),
        ),
        (
            "maturin",
            "sdist",
            (
                "run",
                "maturin",
                "sdist",
                "--manifest-path",
                "rust/cuprum-rust/Cargo.toml",
                "--out",
            ),
        ),
    ],
)
def test_frontend_arguments_name_the_pinned_route(
    tmp_path: Path,
    backend: _Backend,
    archive_format: _Format,
    expected_prefix: tuple[str, ...],
) -> None:
    """Each route runs the pinned frontend with its own output directory."""
    destination = tmp_path / "out"

    arguments = distribution_arguments(backend, archive_format, destination)

    assert arguments[:-1] == expected_prefix, (
        f"{backend} {archive_format} must use its documented frontend command"
    )
    assert arguments[-1] == str(destination), (
        "the archive must land in the route's own destination"
    )


@pytest.mark.parametrize("backend", ["uv", "maturin"])
@pytest.mark.parametrize("archive_format", ["wheel", "sdist"])
def test_frontend_runs_with_an_owned_group_and_its_route_bound(
    tmp_path: Path,
    backend: _Backend,
    archive_format: _Format,
) -> None:
    """Every route is bounded, offline, and terminates its own descendants."""
    request = _request(tmp_path, backend, archive_format)
    archive = request.destination / _ARCHIVE_SUFFIXES[archive_format]
    runner = _RecordingRunner()
    frontend = _frontend(runner, [archive])

    outcome = frontend(request)

    assert outcome == archive, "a single archive is the successful outcome"
    [(arguments, context)] = runner.calls
    assert arguments == distribution_arguments(
        backend, archive_format, archive.parent
    ), "the recorded invocation must be the route's documented command"
    assert context.cwd == tmp_path, "the frontend must run from the repository root"
    assert context.env is not None, "packaging must receive an explicit environment"
    assert context.env["CARGO_NET_OFFLINE"] == "true", (
        "packaging must resolve dependencies against the prepared cache only"
    )
    assert context.env == dict(request.environment), (
        "the route's environment must pass through unchanged"
    )
    assert context.timeout == _DEFAULT_BUILD_TIMEOUTS[backend], (
        "each backend keeps its own end-to-end watchdog"
    )
    assert context.process_group is ProcessGroupPolicy.OWN_GROUP, (
        "the frontend must terminate its descendants when the watchdog expires"
    )


def test_frontend_reports_non_zero_diagnostics(tmp_path: Path) -> None:
    """A failing frontend retains its streams instead of raising."""
    request = _request(tmp_path, "uv", "wheel")
    runner = _RecordingRunner(exit_code=3, stdout="build output", stderr="link failed")

    outcome = _frontend(runner, [])(request)

    assert isinstance(outcome, BuildFailure), "a non-zero exit is a retained failure"
    assert "uv wheel build failed with 3" in outcome.diagnostic, (
        "the diagnostic must name the route and the exit code"
    )
    assert "stdout: build output" in outcome.diagnostic, (
        "the diagnostic must retain captured standard output"
    )
    assert "stderr: link failed" in outcome.diagnostic, (
        "the diagnostic must retain captured error output"
    )


@pytest.mark.parametrize(
    ("archive_count", "expected_fragment"),
    [
        (0, "found []"),
        (2, "found ['cuprum.whl', 'cuprum.whl']"),
    ],
)
def test_frontend_requires_exactly_one_archive(
    tmp_path: Path,
    archive_count: int,
    expected_fragment: str,
) -> None:
    """Zero or several archives is a failure, not an arbitrary pick."""
    request = _request(tmp_path, "uv", "wheel")
    archives = [request.destination / "cuprum.whl" for _ in range(archive_count)]

    outcome = _frontend(_RecordingRunner(), archives)(request)

    assert isinstance(outcome, BuildFailure), (
        "an ambiguous archive count must not be reported as success"
    )
    assert expected_fragment in outcome.diagnostic, (
        "the diagnostic must name the archives it actually found"
    )
