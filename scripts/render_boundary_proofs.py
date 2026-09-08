#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Attach Verus specifications to the actual production length kernels.

Only function signatures acquire specifications; executable bodies are copied
verbatim from production. Generated output belongs under ``rust/target`` and
is regenerated on every proof run, so a stale mirror cannot pass verification.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COUNT_SIGNATURE = "-> Option<usize> {"
PROGRESS_SIGNATURE = "-> Option<(u64, u64)> {"
COUNT_CONTRACT = """-> (result: Option<usize>)
    ensures match result {
        Some(value) => value == count && count <= capacity,
        None => count > capacity,
    },
{"""
PROGRESS_CONTRACT = """-> (result: Option<(u64, u64)>)
    ensures match result {
        Some((new_total, tail)) =>
            new_total as int == total as int + written as int
            && tail as int + written as int == remaining as int,
        None => written > remaining || total as int + written as int > u64::MAX,
    },
{"""


def render(source: str) -> str:
    """Render production functions with specifications and unchanged bodies.

    Parameters
    ----------
    source : str
        Complete production ``progress.rs`` module.

    Returns
    -------
    str
        Standalone Verus input containing the production function bodies.

    Raises
    ------
    ValueError
        If a signature or the test-module boundary changed unexpectedly.
    """
    marker = "#[cfg(test)]"
    if source.count(marker) != 1:
        msg = "production kernel must have exactly one test-module boundary"
        raise ValueError(msg)
    production, _ = source.split(marker, 1)
    for signature in (COUNT_SIGNATURE, PROGRESS_SIGNATURE):
        if production.count(signature) != 1:
            msg = f"production signature changed: {signature}"
            raise ValueError(msg)
    # Verus uses exec functions; const qualification affects compile-time use,
    # not the executable bodies being verified. Inner module attributes cannot
    # occur inside verus!, so the lint remains on the generated crate root.
    production = production.replace("#![forbid(unsafe_code)]\n", "")
    production = production.replace("pub const fn", "pub fn")
    production = production.replace(COUNT_SIGNATURE, COUNT_CONTRACT)
    production = production.replace(PROGRESS_SIGNATURE, PROGRESS_CONTRACT)
    production = production.replace("//!", "//")
    return (
        "#![forbid(unsafe_code)]\nuse vstd::prelude::*;\nverus! {\n"
        + production
        + "}\n"
    )


def main() -> None:
    """Write regenerated Verus input beneath the workspace build directory."""
    source = ROOT / "rust/cuprum-native-io/src/progress.rs"
    destination = ROOT / "rust/target/boundary-verification/progress.rs"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(source.read_text(encoding="utf-8")), encoding="utf-8")
    memory_source = ROOT / "rust/cuprum-native-io/src/memory.rs"
    memory = memory_source.read_text(encoding="utf-8").split("#[cfg(test)]", 1)[0]
    memory = memory.replace("#![forbid(unsafe_code)]", "")
    memory = memory.replace("#[cfg(any(windows, test, kani))]", "").replace("//!", "//")
    assessment = destination.with_name("memory-assessment.rs")
    assessment.write_text(
        "use vstd::prelude::*;\nverus! {\n" + memory + "}\n", encoding="utf-8"
    )

    resource = (ROOT / "rust/cuprum-native-io/src/lib.rs").read_text(encoding="utf-8")
    start = resource.index("pub unsafe fn adopt_writer")
    end = resource.index("/// Borrow a raw reader", start)
    resource_assessment = (
        "use vstd::prelude::*;\n"
        "use std::os::fd::{OwnedFd as OwnedStream, FromRawFd};\n"
        "type PlatformFd = i32;\nverus! {\n" + resource[start:end] + "}\n"
    )
    destination.with_name("resource-assessment.rs").write_text(
        resource_assessment, encoding="utf-8"
    )


if __name__ == "__main__":
    main()
