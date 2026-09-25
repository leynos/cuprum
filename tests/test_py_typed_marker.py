"""The PEP 561 marker must reach every distribution a user can install.

PEP 561 requires a ``py.typed`` marker inside the installed package before a
type checker will read that package's annotations. Cuprum annotates its whole
public surface, so without the marker every downstream type checker silently
treats ``cuprum`` as untyped and falls back to ``Any``.

The marker is packaging metadata rather than source, so nothing in the unit
suite notices when a build backend stops copying it: a wheel that lost the file
still imports, still passes every behavioural test, and still reports success.
These tests therefore inspect real built artefacts and assert the marker is
inside them, from both the ``uv_build`` frontend and the ``maturin`` frontend,
for both a wheel and a source distribution.

The native wheel's marker is covered from the other side as well:
``test_maturin_wheel_build_snapshot`` in
``cuprum/unittests/test_maturin_build.py`` compares the whole packaged file list
against a syrupy snapshot, so the marker cannot arrive in that wheel unrecorded.
"""

from __future__ import annotations

import tarfile
import typing as typ
import zipfile
from pathlib import Path

import pytest

from cuprum import Program, ProgramCatalogue, ScopeConfig, scoped, sh
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult

#: The marker's path inside an installed package, as PEP 561 requires it.
MARKER_PATH = "cuprum/py.typed"
#: The one content PEP 561 gives a meaning to. A marker holding this word
#: declares partial typing; anything else, including an empty file, declares
#: the package fully typed.
PARTIAL_MARKER_CONTENT = "partial"
#: Maturin spells the wheel build ``build``; ``uv`` spells it ``--wheel``.
_MATURIN_SUBCOMMANDS = {"wheel": "build", "sdist": "sdist"}
_ARCHIVE_SUFFIXES = {"wheel": "*.whl", "sdist": "*.tar.gz"}


def _run_packaging_frontend(
    arguments: tuple[str, ...],
) -> CommandResult:
    """Run one packaging command through Cuprum's own safe-command layer."""
    program = Program("uv")
    catalogue = ProgramCatalogue.from_programs(
        program,
        name="py-typed-distribution",
        documentation_locations=("docs/developers-guide.md",),
    )
    command = sh.make(program, catalogue=catalogue)
    with scoped(ScopeConfig(allowlist=frozenset({program}))):
        return command(*arguments).run_sync(
            context=sh.ExecutionContext(cwd=repo_root(), timeout=180)
        )


def _distribution_arguments(
    backend: str, kind: str, destination: Path
) -> tuple[str, ...]:
    """Return the command line that builds one distribution with one frontend."""
    if backend == "uv":
        return ("build", f"--{kind}", "--out-dir", str(destination))
    return (
        "run",
        "maturin",
        _MATURIN_SUBCOMMANDS[kind],
        "--manifest-path",
        "rust/cuprum-rust/Cargo.toml",
        "--out",
        str(destination),
    )


def _build_distribution(backend: str, kind: str, destination: Path) -> Path:
    """Build one archive and return it, failing the test unless exactly one appears.

    Parameters
    ----------
    backend : str
        ``"uv"`` for the ``uv_build`` frontend, or ``"maturin"``.
    kind : str
        ``"wheel"`` or ``"sdist"``.
    destination : Path
        Directory the archive is written to.

    Returns
    -------
    Path
        The single archive the build produced.
    """
    suffix = _ARCHIVE_SUFFIXES[kind]
    result = _run_packaging_frontend(
        _distribution_arguments(backend, kind, destination)
    )
    assert result.exit_code == 0, (
        f"{backend} {kind} build failed with {result.exit_code}: {result.stderr}"
    )
    archives = sorted(destination.glob(suffix))
    assert len(archives) == 1, (
        f"{backend} {kind} build must produce exactly one {suffix} archive, "
        f"found {[archive.name for archive in archives]}"
    )
    return archives[0]


def _wheel_marker(wheel_path: Path) -> str | None:
    """Return the wheel's marker text, or ``None`` when the wheel omits it.

    Returns
    -------
    str | None
        The decoded marker contents, or ``None`` if the wheel lacks it. An
        empty string is a present but empty marker and is distinct from
        ``None``, which matters because an empty marker is the declaration
        PEP 561 gives to a fully typed package.
    """
    with zipfile.ZipFile(wheel_path) as archive:
        if MARKER_PATH not in archive.namelist():
            return None
        return archive.read(MARKER_PATH).decode("utf-8")


@pytest.mark.parametrize("backend", ["uv", "maturin"])
def test_wheel_ships_the_pep561_marker(backend: str, tmp_path: Path) -> None:
    """Every wheel carries an empty, complete ``py.typed`` marker.

    The native wheel is built here even though
    ``test_maturin_wheel_build_snapshot`` already inspects one: that snapshot
    records which files are present, while this test asserts the marker's
    *content*, which decides whether a type checker reads the annotations at
    all or reads them as a partial stub.
    """
    wheel_path = _build_distribution(backend, "wheel", tmp_path)
    marker = _wheel_marker(wheel_path)

    assert marker is not None, (
        f"{backend} wheel {wheel_path.name} is missing {MARKER_PATH}; without it "
        "every downstream type checker treats cuprum as untyped"
    )
    assert not marker.strip(), (
        f"{backend} wheel holds {marker!r} in {MARKER_PATH}; only the literal "
        f"{PARTIAL_MARKER_CONTENT!r} carries any meaning under PEP 561, and "
        "cuprum annotates its whole public surface, so the marker must be empty"
    )


@pytest.mark.parametrize("backend", ["uv", "maturin"])
def test_source_distribution_ships_the_pep561_marker(
    backend: str,
    tmp_path: Path,
) -> None:
    """Both source archives carry the marker, so a from-source install stays typed.

    A wheel-only guarantee would be incomplete: ``pip install --no-binary
    cuprum cuprum`` builds from the sdist, so the marker must travel in the
    archive that build unpacks.
    """
    archive_path = _build_distribution(backend, "sdist", tmp_path)

    with tarfile.open(archive_path) as archive:
        # Both frontends nest the tree under a single ``cuprum-<version>/``
        # root, so drop it: PEP 561 constrains where the marker sits inside the
        # package, not what the archive's own root directory is called.
        members = {Path(name).parts[1:] for name in archive.getnames()}

    assert tuple(MARKER_PATH.split("/")) in members, (
        f"{backend} sdist is missing {MARKER_PATH}; a source install would "
        "produce an untyped package"
    )
