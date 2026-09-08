#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = []
# [tool.ty.environment]
# extra-paths = ["."]
# ///
"""Install the prebuilt Z3 release required by the pinned Verus binary.

Verus's September 2026 archive does not include the solver. The version follows
its upstream ``source/tools/get-z3.sh``; the SHA-256 is the release asset digest.
"""

import zipfile

from install_boundary_kani import ROOT, checked_download

NAME = "z3-4.16.0-x64-glibc-2.39"
DIGEST = "7288c49a5bd6dbafd7b0b0d1f65956b91672da24b08f09242919af159be3418e"
URL = f"https://github.com/Z3Prover/z3/releases/download/z3-4.16.0/{NAME}.zip"


def main() -> None:
    """Install only the verified solver executable in the repository cache."""
    cache = ROOT / ".cache/boundary-z3"
    cache.mkdir(parents=True, exist_ok=True)
    archive_path = cache / f"{NAME}.zip"
    checked_download(URL, archive_path, DIGEST)
    binary = cache / "z3"
    with zipfile.ZipFile(archive_path) as archive:
        binary.write_bytes(archive.read(f"{NAME}/bin/z3"))
    binary.chmod(0o755)


if __name__ == "__main__":
    main()
