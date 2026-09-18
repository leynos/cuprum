"""Guard the correspondence between Verus inputs and production kernels."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import render_boundary_proofs as renderer
from scripts.tests.boundary_harness_support import copy_boundary_repository

SOURCE = Path(__file__).resolve().parents[2] / "rust/cuprum-native-io/src/progress.rs"
MEMORY = Path(__file__).resolve().parents[2] / "rust/cuprum-native-io/src/memory.rs"


def test_production_fault_reaches_verus_input() -> None:
    """A production accounting fault must survive rendering unchanged."""
    source = SOURCE.read_text(encoding="utf-8")
    mutant = source.replace("total + written", "total - written")
    assert mutant != source, "the deliberate fault no longer matches production"
    generated = renderer.render(mutant)
    assert "Some((total - written, remaining - written))" in generated, (
        "renderer erased the production fault"
    )
    assert "new_total as int == total as int + written as int" in generated, (
        "fault changed the specification instead of the implementation"
    )


@pytest.mark.parametrize(
    "signature", [renderer.COUNT_SIGNATURE, renderer.PROGRESS_SIGNATURE]
)
def test_changed_signature_fails_closed(signature: str) -> None:
    """An unrecognized signature must not silently lose its specification."""
    source = SOURCE.read_text(encoding="utf-8").replace(signature, "-> bool {")
    with pytest.raises(ValueError, match="production signature changed"):
        renderer.render(source)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("pub const fn count() {}", id="absent"),
        pytest.param(
            "#[cfg(test)]\nmod first;\n#[cfg(test)]\nmod second;\n",
            id="duplicated",
        ),
    ],
)
def test_ambiguous_test_boundary_fails_closed(source: str) -> None:
    """An absent or duplicated marker must not silently truncate the module."""
    with pytest.raises(ValueError, match="exactly one test-module boundary"):
        renderer._production_half(source)


def test_memory_assessment_carries_no_test_module() -> None:
    """The memory proof renders production retention, not its tests."""
    production = renderer._production_half(MEMORY.read_text(encoding="utf-8"))
    assert "with_retained_owner" in production, (
        "the memory proof lost the retention kernel it exists to verify"
    )
    assert "#[cfg(test)]" not in production, "the test module leaked into the proof"


def test_main_writes_every_assessment_without_test_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() emits all three Verus inputs from production source alone."""
    root = copy_boundary_repository(tmp_path)
    monkeypatch.setattr(renderer, "ROOT", root)
    renderer.main()

    destination = root / "rust/target/boundary-verification"
    generated = {
        path.name: path.read_text(encoding="utf-8") for path in destination.iterdir()
    }
    assert sorted(generated) == [
        "memory-assessment.rs",
        "progress.rs",
        "resource-assessment.rs",
    ], "every assessment must be regenerated beneath the build directory"
    for name, text in generated.items():
        assert "#[cfg(test)]" not in text, f"{name} carries a test module"
        assert "use vstd::prelude::*;" in text, f"{name} is not a Verus input"
    assert "total + written" in generated["progress.rs"], (
        "the progress proof must carry the production accounting"
    )
    assert "with_retained_owner" in generated["memory-assessment.rs"], (
        "the memory proof must carry the production retention kernel"
    )
    assert "adopt_writer" in generated["resource-assessment.rs"], (
        "the resource proof must carry the production adoption helper"
    )
    assert "Borrow a raw reader" not in generated["resource-assessment.rs"], (
        "the resource proof must stop at the reader helper's documentation"
    )


def test_executable_bodies_are_preserved() -> None:
    """Removing only the added contracts recovers the production module."""
    source = SOURCE.read_text(encoding="utf-8").split("#[cfg(test)]", 1)[0]
    generated = renderer.render(SOURCE.read_text(encoding="utf-8"))
    recovered = generated.removeprefix(
        "#![forbid(unsafe_code)]\nuse vstd::prelude::*;\nverus! {\n"
    ).removesuffix("}\n")
    recovered = recovered.replace(renderer.COUNT_CONTRACT, renderer.COUNT_SIGNATURE)
    recovered = recovered.replace(
        renderer.PROGRESS_CONTRACT, renderer.PROGRESS_SIGNATURE
    )
    expected = (
        source
        .replace("#![forbid(unsafe_code)]\n", "")
        .replace("pub const fn", "pub fn")
        .replace("//!", "//")
    )
    assert recovered == expected, "Verus executable bodies differ from production"
