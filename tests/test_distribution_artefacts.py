"""Check the invocation-local distribution build cache."""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest

from tests.helpers import distribution_artefacts
from tests.helpers.distribution_artefacts import (
    DISTRIBUTION_BUILD_TIMEOUT_SECONDS,
    DISTRIBUTION_TEST_TIMEOUT_SECONDS,
    DistributionArtefactFactory,
    _configured_build_timeout_seconds,
    _FactorySettings,
)
from tests.helpers.grouped_recipe_flow import require_grouped_selector_drives_pytest
from tests.helpers.makefile import recipe_of, variable_expansion
from tests.helpers.suite_selection import selected_paths

_FRONTEND_TIMEOUT_MESSAGE = "frontend exceeded its limit"
#: Cold Maturin wheel build measured on a loaded developer host on 2026-10-30.
_MEASURED_COLD_MATURIN_SECONDS = 163


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
    backend: typ.Literal["uv", "maturin"],
    expected_seconds: int,
) -> None:
    """The override adds headroom and never lowers a backend bound."""
    assert (
        _configured_build_timeout_seconds(configured_value, backend=backend)
        == expected_seconds
    ), "the packaging command must use the larger of override and backend default"


@pytest.mark.parametrize("configured_value", ["0", "-1", "invalid"])
def test_packaging_timeout_rejects_invalid_override(
    configured_value: str,
) -> None:
    """Invalid frontend limits fail clearly instead of disabling the watchdog."""
    with pytest.raises(ValueError, match="CUPRUM_PACKAGING_BUILD_TIMEOUT"):
        _configured_build_timeout_seconds(configured_value, backend="uv")


def test_packaging_test_timeout_covers_frontend_limit_with_margin() -> None:
    """Consumer tests leave 60 seconds beyond the largest frontend limit."""
    largest_frontend_limit = max(DISTRIBUTION_BUILD_TIMEOUT_SECONDS.values())
    assert DISTRIBUTION_TEST_TIMEOUT_SECONDS - largest_frontend_limit == 60, (
        "the consumer timeout must stay 60 seconds above the largest build cap"
    )


def test_maturin_budget_keeps_headroom_over_its_measured_cold_build() -> None:
    """The native watchdog leaves room for a cold build and admission waits."""
    maturin_limit = DISTRIBUTION_BUILD_TIMEOUT_SECONDS["maturin"]
    assert maturin_limit > DISTRIBUTION_BUILD_TIMEOUT_SECONDS["uv"], (
        "the native build must keep a larger bound than the Python-only backend"
    )
    assert maturin_limit >= 3 * _MEASURED_COLD_MATURIN_SECONDS, (
        "the Maturin watchdog must retain headroom for a cold target and the "
        "compiler-admission waits no workload lease can exclude"
    )


def _new_factory(tmp_path: Path) -> DistributionArtefactFactory:
    """Create a test factory with one isolated directory per concern."""
    return DistributionArtefactFactory(
        _FactorySettings(
            repository_root=tmp_path,
            artefacts_root=tmp_path / "artefacts",
            target_root=tmp_path / "target",
            command_environment={},
            timings_path=tmp_path / "timings.jsonl",
        )
    )


def test_factory_reuses_a_successful_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated requests for a route and format share the same archive path."""
    archive = tmp_path / "cuprum.whl"
    calls: list[tuple[str, str]] = []

    def build(request: distribution_artefacts._BuildRequest) -> Path:
        """Return the controlled archive and record the requested build key."""
        calls.append((request.backend, request.archive_format))
        return archive

    monkeypatch.setattr(distribution_artefacts, "_build_distribution", build)
    factory = _new_factory(tmp_path)

    first = factory("uv", "wheel")
    second = factory("uv", "wheel")

    assert first == archive, "the initial build must return its archive path"
    assert second == first, "a warm request must reuse the cached archive"
    assert calls == [("uv", "wheel")], "one cache key must invoke the frontend once"


def test_factory_reuses_the_original_failure_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed route is not rebuilt and keeps its first diagnostic."""
    calls = 0
    diagnostic = "maturin sdist failed: compiler output"

    def fail(_request: distribution_artefacts._BuildRequest) -> Path:
        """Raise one controlled failure for both cache requests."""
        nonlocal calls
        calls += 1
        raise RuntimeError(diagnostic)

    monkeypatch.setattr(distribution_artefacts, "_build_distribution", fail)
    factory = _new_factory(tmp_path)

    for attempt in range(2):
        with pytest.raises(RuntimeError, match="compiler output") as error:
            factory("maturin", "sdist")
        assert str(error.value) == (
            f"maturin sdist build raised RuntimeError: {diagnostic}"
        ), f"attempt {attempt + 1} must report the cached original diagnostics"

    assert calls == 1, "a cached failure must not rerun the packaging frontend"


def test_factory_keeps_captured_output_from_a_timed_out_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later consumer sees the first timeout's captured compiler output."""

    class CapturedTimeoutError(TimeoutError):
        """Provide the same stream fields as Cuprum's subprocess timeout."""

        stdout = b"compiler was waiting"
        stderr = b"resource queue details"

    def fail(_request: distribution_artefacts._BuildRequest) -> Path:
        """Raise a timeout carrying the captured compiler streams."""
        raise CapturedTimeoutError(_FRONTEND_TIMEOUT_MESSAGE)

    monkeypatch.setattr(distribution_artefacts, "_build_distribution", fail)
    factory = _new_factory(tmp_path)

    with pytest.raises(RuntimeError) as error:
        factory("maturin", "wheel")

    assert _FRONTEND_TIMEOUT_MESSAGE in str(error.value), (
        "the cached failure must preserve the exception message"
    )
    assert "stdout: compiler was waiting" in str(error.value), (
        "the cached failure must retain captured standard output"
    )
    assert "stderr: resource queue details" in str(error.value), (
        "the cached failure must retain captured error output"
    )


def test_factory_uses_independent_keys_for_backend_and_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two frontends and archive formats keep separate build outcomes."""
    calls: list[tuple[str, str]] = []

    def build(request: distribution_artefacts._BuildRequest) -> Path:
        """Return the backend-specific archive path for a build key."""
        calls.append((request.backend, request.archive_format))
        return tmp_path / request.backend / f"cuprum.{request.archive_format}"

    monkeypatch.setattr(distribution_artefacts, "_build_distribution", build)
    factory = _new_factory(tmp_path)

    for backend in ("uv", "maturin"):
        for archive_format in ("wheel", "sdist"):
            artefact = factory(backend, archive_format)
            assert artefact == tmp_path / backend / f"cuprum.{archive_format}", (
                f"{backend} {archive_format} must retain its own artefact"
            )

    assert calls.count(("uv", "wheel")) == 1, "uv wheel must build exactly once"
    assert calls.count(("uv", "sdist")) == 1, "uv sdist must build exactly once"
    assert calls.count(("maturin", "wheel")) == 1, (
        "maturin wheel must build exactly once"
    )
    assert calls.count(("maturin", "sdist")) == 1, (
        "maturin sdist must build exactly once"
    )


def test_factory_assigns_a_target_directory_to_each_build_configuration(
    tmp_path: Path,
) -> None:
    """Packaging routes cannot write incompatible outputs into one target."""
    target_root = tmp_path / "target"
    factory = DistributionArtefactFactory(
        _FactorySettings(
            repository_root=tmp_path,
            artefacts_root=tmp_path / "artefacts",
            target_root=target_root,
            command_environment={},
            timings_path=tmp_path / "timings.jsonl",
        )
    )

    targets = {
        (backend, archive_format): Path(
            factory._build_environment(backend, archive_format)["CARGO_TARGET_DIR"]
        )
        for backend in ("uv", "maturin")
        for archive_format in ("wheel", "sdist")
    }

    assert len(set(targets.values())) == 4, (
        "each backend and format must use its compatible target configuration"
    )
    assert all(target.is_dir() for target in targets.values()), (
        "a route must own its target directory before Cargo starts"
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
