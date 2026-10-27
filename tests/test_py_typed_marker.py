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

if typ.TYPE_CHECKING:
    from tests.helpers.distribution_artefacts import DistributionArtefactFactory

#: The marker's path inside an installed package, as PEP 561 requires it.
MARKER_PATH = "cuprum/py.typed"
#: The one content PEP 561 gives a meaning to. A marker holding this word
#: declares partial typing; anything else, including an empty file, declares
#: the package fully typed.
PARTIAL_MARKER_CONTENT = "partial"


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


# The test budget includes the first lazy build and its archive assertions.
@pytest.mark.timeout(240, func_only=True)
@pytest.mark.parametrize("backend", ["uv", "maturin"])
def test_wheel_ships_the_pep561_marker(
    backend: typ.Literal["uv", "maturin"],
    distribution_artefact_factory: DistributionArtefactFactory,
) -> None:
    """Every wheel carries an empty, complete ``py.typed`` marker.

    The native wheel is built here even though
    ``test_maturin_wheel_build_snapshot`` already inspects one: that snapshot
    records which files are present, while this test asserts the marker's
    *content*, which decides whether a type checker reads the annotations at
    all or reads them as a partial stub.
    """
    wheel_path = distribution_artefact_factory(backend, "wheel")
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


# The test budget includes the first lazy build and its archive assertions.
@pytest.mark.timeout(240, func_only=True)
@pytest.mark.parametrize("backend", ["uv", "maturin"])
def test_source_distribution_ships_the_pep561_marker(
    backend: typ.Literal["uv", "maturin"],
    distribution_artefact_factory: DistributionArtefactFactory,
) -> None:
    """Both source archives carry the marker, so a from-source install stays typed.

    A wheel-only guarantee would be incomplete: ``pip install --no-binary
    cuprum cuprum`` builds from the sdist, so the marker must travel in the
    archive that build unpacks.
    """
    archive_path = distribution_artefact_factory(backend, "sdist")

    with tarfile.open(archive_path) as archive:
        # Both frontends nest the tree under a single ``cuprum-<version>/``
        # root, so drop it: PEP 561 constrains where the marker sits inside the
        # package, not what the archive's own root directory is called.
        members = {Path(name).parts[1:] for name in archive.getnames()}

    assert tuple(MARKER_PATH.split("/")) in members, (
        f"{backend} sdist is missing {MARKER_PATH}; a source install would "
        "produce an untyped package"
    )
