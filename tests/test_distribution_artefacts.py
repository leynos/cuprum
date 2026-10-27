"""Check the invocation-local distribution build cache."""

from __future__ import annotations

import fnmatch
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

_FRONTEND_TIMEOUT_MESSAGE = "frontend exceeded its limit"


@pytest.mark.parametrize(
    ("configured_value", "expected_seconds"),
    [(None, 180), ("300", 300)],
)
def test_packaging_timeout_uses_positive_override_or_default(
    configured_value: str | None,
    expected_seconds: int,
) -> None:
    """A configured frontend limit is positive and defaults to 180 seconds."""
    assert _configured_build_timeout_seconds(configured_value) == expected_seconds, (
        "the packaging command must use configured seconds or its default"
    )


@pytest.mark.parametrize("configured_value", ["0", "-1", "invalid"])
def test_packaging_timeout_rejects_invalid_override(
    configured_value: str,
) -> None:
    """Invalid frontend limits fail clearly instead of disabling the watchdog."""
    with pytest.raises(ValueError, match="CUPRUM_PACKAGING_BUILD_TIMEOUT"):
        _configured_build_timeout_seconds(configured_value)


def test_packaging_test_timeout_covers_frontend_limit_with_margin() -> None:
    """Consumer tests leave 60 seconds beyond the configured frontend limit."""
    assert (
        DISTRIBUTION_TEST_TIMEOUT_SECONDS - DISTRIBUTION_BUILD_TIMEOUT_SECONDS == 60
    ), "the consumer timeout must stay 60 seconds above the build cap"


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
    selected_targets = set(variable_expansion("PYTEST_TARGETS"))
    missing_targets = {
        target
        for target in expected
        if not any(
            fnmatch.fnmatchcase(target, selector) for selector in selected_targets
        )
    }
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
