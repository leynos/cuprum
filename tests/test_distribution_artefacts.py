"""Check the invocation-local distribution cache and its read-only query seam."""

from __future__ import annotations

import json
import time
import typing as typ
from pathlib import Path

import pytest

from tests.helpers.distribution_artefacts import (
    ArtefactNotBuiltError,
    DistributionArtefactFactory,
    DistributionArtefactSettings,
)
from tests.helpers.distribution_build import (
    BuildFailure,
    BuildRequest,
    PathFileSystem,
)
from tests.helpers.grouped_recipe_flow import require_grouped_selector_drives_pytest
from tests.helpers.makefile import recipe_of, variable_expansion
from tests.helpers.suite_selection import selected_paths

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type _Backend = typ.Literal["uv", "maturin"]
type _Format = typ.Literal["wheel", "sdist"]

_FRONTEND_TIMEOUT_MESSAGE = "frontend exceeded its limit"


class _RecordingFrontend:
    """Record each build request and answer it with a controlled outcome."""

    def __init__(
        self,
        answer: cabc.Callable[[BuildRequest], Path | BuildFailure] | None = None,
    ) -> None:
        """Store the answer, defaulting to a notional archive per route."""
        self.requests: list[BuildRequest] = []
        self._answer = answer or _archive_for

    def __call__(self, request: BuildRequest) -> Path | BuildFailure:
        """Record the request and return the configured outcome."""
        self.requests.append(request)
        return self._answer(request)


class _RecordingFileSystem:
    """Record every filesystem command while applying it for real."""

    def __init__(self) -> None:
        """Create the recording wrapper over the real filesystem."""
        self.calls: list[tuple[str, Path]] = []
        self._real = PathFileSystem()

    def prepare(self, directory: Path) -> None:
        """Record and apply one directory-creation command."""
        self.calls.append(("prepare", directory))
        self._real.prepare(directory)

    def archives(self, directory: Path, pattern: str) -> list[Path]:
        """Record and apply one archive scan."""
        self.calls.append(("archives", directory))
        return self._real.archives(directory, pattern)

    def append_line(self, path: Path, line: str) -> None:
        """Record and apply one timing append."""
        self.calls.append(("append_line", path))
        self._real.append_line(path, line)


class _CountingClock:
    """Count clock reads so a query's silence is observable."""

    def __init__(self) -> None:
        """Start with no reads recorded."""
        self.reads = 0

    def __call__(self) -> float:
        """Record the read and advance a monotonic double."""
        self.reads += 1
        return float(self.reads)


def _archive_for(request: BuildRequest) -> Path:
    """Return a notional archive path for one build request."""
    return request.destination / f"cuprum.{request.archive_format}"


def _factory(
    tmp_path: Path,
    *,
    frontend: _RecordingFrontend | None = None,
    filesystem: _RecordingFileSystem | None = None,
    clock: cabc.Callable[[], float] = time.monotonic,
) -> DistributionArtefactFactory:
    """Create a test factory with one isolated directory per concern."""
    return DistributionArtefactFactory(
        DistributionArtefactSettings(
            repository_root=tmp_path,
            artefacts_root=tmp_path / "artefacts",
            target_root=tmp_path / "target",
            environment={},
            timings_path=tmp_path / "timings.jsonl",
            frontend=frontend or _RecordingFrontend(),
            filesystem=filesystem or _RecordingFileSystem(),
            clock=clock,
        )
    )


def test_build_reuses_a_successful_build(tmp_path: Path) -> None:
    """Repeated requests for a route and format share the same archive path."""
    frontend = _RecordingFrontend()
    factory = _factory(tmp_path, frontend=frontend)

    first = factory.build_artefact("uv", "wheel")
    second = factory.build_artefact("uv", "wheel")

    assert first == tmp_path / "artefacts/uv/wheel/cuprum.wheel", (
        "the initial build must return its archive path"
    )
    assert second == first, "a warm request must reuse the cached archive"
    assert len(frontend.requests) == 1, "one cache key must invoke the frontend once"


def test_build_reuses_the_original_failure_diagnostic(tmp_path: Path) -> None:
    """A failed route is not rebuilt and keeps its first diagnostic."""
    diagnostic = "maturin sdist failed: compiler output"

    def fail(_request: BuildRequest) -> Path:
        """Raise one controlled failure for both cache requests."""
        raise RuntimeError(diagnostic)

    frontend = _RecordingFrontend(fail)
    factory = _factory(tmp_path, frontend=frontend)

    for attempt in range(2):
        with pytest.raises(RuntimeError, match="compiler output") as error:
            factory.build_artefact("maturin", "sdist")
        assert str(error.value) == (
            f"maturin sdist build raised RuntimeError: {diagnostic}"
        ), f"attempt {attempt + 1} must report the cached original diagnostics"

    assert len(frontend.requests) == 1, "a cached failure must not rerun the frontend"


def test_build_keeps_captured_output_from_a_timed_out_build(tmp_path: Path) -> None:
    """A later consumer sees the first timeout's captured compiler output."""

    class CapturedTimeoutError(TimeoutError):
        """Provide the same stream fields as Cuprum's subprocess timeout."""

        stdout = b"compiler was waiting"
        stderr = b"resource queue details"

    def fail(_request: BuildRequest) -> Path:
        """Raise a timeout carrying the captured compiler streams."""
        raise CapturedTimeoutError(_FRONTEND_TIMEOUT_MESSAGE)

    factory = _factory(tmp_path, frontend=_RecordingFrontend(fail))

    with pytest.raises(RuntimeError) as error:
        factory.build_artefact("maturin", "wheel")

    assert _FRONTEND_TIMEOUT_MESSAGE in str(error.value), (
        "the cached failure must preserve the exception message"
    )
    assert "stdout: compiler was waiting" in str(error.value), (
        "the cached failure must retain captured standard output"
    )
    assert "stderr: resource queue details" in str(error.value), (
        "the cached failure must retain captured error output"
    )


def test_build_uses_independent_keys_for_backend_and_format(tmp_path: Path) -> None:
    """The two frontends and archive formats keep separate build outcomes."""
    frontend = _RecordingFrontend()
    factory = _factory(tmp_path, frontend=frontend)

    for backend in ("uv", "maturin"):
        for archive_format in ("wheel", "sdist"):
            artefact = factory.build_artefact(backend, archive_format)
            expected = (
                tmp_path
                / "artefacts"
                / backend
                / archive_format
                / f"cuprum.{archive_format}"
            )
            assert artefact == expected, (
                f"{backend} {archive_format} must retain its own artefact"
            )

    assert len(frontend.requests) == 4, "each backend and format must build once"


def test_build_assigns_a_target_directory_to_each_configuration(
    tmp_path: Path,
) -> None:
    """Packaging routes cannot write incompatible outputs into one target."""
    frontend = _RecordingFrontend()
    factory = _factory(tmp_path, frontend=frontend)

    for backend in ("uv", "maturin"):
        for archive_format in ("wheel", "sdist"):
            factory.build_artefact(backend, archive_format)

    targets = {
        (request.backend, request.archive_format): Path(
            request.environment["CARGO_TARGET_DIR"]
        )
        for request in frontend.requests
    }
    assert len(set(targets.values())) == 4, (
        "each backend and format must use its compatible target configuration"
    )
    assert all(target.is_dir() for target in targets.values()), (
        "a route must own its target directory before the frontend starts"
    )


def test_artefact_path_is_a_read_only_query(tmp_path: Path) -> None:
    """Asking where an artefact lives must not build, create, or time anything."""
    filesystem = _RecordingFileSystem()
    clock = _CountingClock()
    frontend = _RecordingFrontend()
    factory = _factory(tmp_path, frontend=frontend, filesystem=filesystem, clock=clock)

    with pytest.raises(ArtefactNotBuiltError, match="uv wheel"):
        factory.artefact_path("uv", "wheel")

    assert not filesystem.calls, "a lookup must not touch the filesystem"
    assert not frontend.requests, "a lookup must not run the frontend"
    assert clock.reads == 0, "a lookup must not read the clock"
    assert not (tmp_path / "artefacts").exists(), "a lookup must not create directories"

    archive = factory.build_artefact("uv", "wheel")
    calls_after_build = list(filesystem.calls)
    reads_after_build = clock.reads

    assert factory.artefact_path("uv", "wheel") == archive, (
        "the query must return the archive its build produced"
    )
    assert filesystem.calls == calls_after_build, (
        "a query after a build must still leave the filesystem untouched"
    )
    assert clock.reads == reads_after_build, (
        "a query after a build must still leave the clock unread"
    )


def test_artefact_path_reports_a_retained_failure(tmp_path: Path) -> None:
    """A query for a failed build carries the diagnostic the build retained."""

    def fail(_request: BuildRequest) -> Path | BuildFailure:
        """Return a retained failure instead of an archive."""
        return BuildFailure("uv wheel build failed with 3")

    factory = _factory(tmp_path, frontend=_RecordingFrontend(fail))

    with pytest.raises(RuntimeError, match="uv wheel build failed with 3"):
        factory.build_artefact("uv", "wheel")

    with pytest.raises(RuntimeError, match="uv wheel build failed with 3"):
        factory.artefact_path("uv", "wheel")


def test_build_records_one_duration_per_key(tmp_path: Path) -> None:
    """Each build appends exactly one non-secret timing record."""
    factory = _factory(tmp_path)

    factory.build_artefact("uv", "wheel")
    factory.build_artefact("uv", "wheel")
    factory.build_artefact("maturin", "sdist")

    records = [
        json.loads(line)
        for line in (tmp_path / "timings.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(record["backend"], record["format"]) for record in records] == [
        ("uv", "wheel"),
        ("maturin", "sdist"),
    ], "the warm request must not add a second record"
    assert all(record["phase"] == "packaging_build" for record in records), (
        "packaging records must be distinguishable from dependency preparation"
    )


def test_makefile_groups_both_distribution_consumers() -> None:
    """One pytest invocation lets both consumers reuse session artefacts."""
    grouped_targets = set(variable_expansion("PYTEST_GROUPED_TARGETS"))
    expected = {
        "tests/test_native_sdist.py",
        "tests/test_py_typed_marker.py",
        "cuprum/unittests/test_doctest_warning_contract.py",
    }
    assert grouped_targets == expected, (
        "the grouped selector must include both archive consumers and the "
        "prepared-home doctest route, "
        f"got {sorted(grouped_targets)}"
    )
    # Resolve the selector the way `make test-python` does: a shell glob, where
    # `*` does not cross a directory separator. `fnmatch` would let a broad
    # pattern such as `cuprum/*.py` stand in for a module pytest never collects.
    selected_targets = {
        path.as_posix() for path in selected_paths(variable_expansion("PYTEST_TARGETS"))
    }
    missing_targets = expected - selected_targets
    assert not missing_targets, (
        "every shared-artefact consumer must remain selected by test-python"
        f": {sorted(missing_targets)}"
    )

    recipe = recipe_of("test-python")
    require_grouped_selector_drives_pytest(
        recipe,
        grouped_targets="PYTEST_GROUPED_TARGETS",
        selected_targets="PYTEST_SELECTED_TARGETS",
        pytest_variable="PYTEST",
    )
