"""Pin provenance and the Makefile guard around the shared verifier toolchain.

One git pin decides which verifier binaries are downloaded. These tests keep it
a full commit hash, keep the Makefile reading it from the file the CI cache key
hashes, and prove that an unreadable pin stops the install instead of letting
`uv` fall back to a package-index name.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed tool argv.
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
PROVER_TOOLS_PIN = REPO_ROOT / "tools/prover-tools/VERSION"

_PIN_PREFIX = "git+https://github.com/leynos/rust-prover-tools@"
_SOURCE_ASSIGNMENT = (
    "PROVER_TOOLS_SOURCE ?= $(strip $(file < tools/prover-tools/VERSION))"
)


def test_prover_tools_pin_is_an_immutable_commit() -> None:
    """A branch or tag could move under the verifier install; a commit cannot."""
    value = PROVER_TOOLS_PIN.read_text(encoding="utf-8").strip()
    assert value.startswith(_PIN_PREFIX), "the shared dependency must stay a git pin"
    revision = value[len(_PIN_PREFIX) :]
    assert re.fullmatch(r"[0-9a-f]{40}", revision), (
        "the shared dependency must be pinned to a full commit hash"
    )


def test_the_makefile_reads_the_pin_from_the_hashed_file() -> None:
    """The cache key hashes the pin file, so the Makefile must be its reader."""
    assert _SOURCE_ASSIGNMENT in MAKEFILE.read_text(encoding="utf-8"), (
        "the Makefile must take the pin from the file the cache key hashes"
    )


def test_an_empty_pin_stops_the_install(tmp_path: Path) -> None:
    """A missing pin must fail rather than let uv resolve a package name."""
    command = [
        shutil.which("make") or "make",
        "-C",
        str(tmp_path),
        "-f",
        str(MAKEFILE),
        "-n",
        "install-verus",
    ]
    # `?=` treats an inherited empty value as a definition, so clear the name to
    # let the guard see the empty `$(file < ...)` this test is about.
    env = {
        name: value
        for name, value in os.environ.items()
        if name != "PROVER_TOOLS_SOURCE"
    }
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        command,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode != 0, "an empty pin must fail the target"
    assert "PROVER_TOOLS_SOURCE is empty" in result.stderr, (
        "the failure must name the pin rather than reaching uv"
    )
