"""Stand-ins for ``gh`` and ``curl`` that hold release state in directories.

``release.yml``'s reconciliation steps talk to two destinations: the GitHub
Release through ``gh`` and PyPI through ``curl``. The tests replace both with
this program, run as ``python release_fake_tools.py gh|curl ARGS...``, so the
checked-in step scripts execute unchanged against state the test controls:

- ``FAKE_GITHUB_ASSETS``: a directory holding one file per release asset.
- ``FAKE_PYPI_FILES``: a directory holding one file per PyPI file.
- ``FAKE_CALLS``: a file each call appends its argument vector to, as JSON.
- ``FAKE_CURL_STATUSES``: comma-separated HTTP statuses the index returns in
  turn; the last repeats. Defaults to ``200``.

Like the real services, neither stand-in ever overwrites a file it holds: a
second upload of an existing asset name fails, as ``gh`` does without
``--clobber``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

#: Where the fake PyPI index says each file can be downloaded from.
FILES_URL = "https://files.example.test/packages/"
_INDEX_URL = "https://pypi.org/simple/cuprum/"


def _digest(path: Path) -> str:
    """Return the SHA-256 hex digest of one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _directory(variable: str) -> Path:
    """Return the state directory named by ``variable``, creating it."""
    directory = Path(os.environ[variable])
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _record(tool: str, args: list[str]) -> None:
    """Append one call to the shared call log."""
    with Path(os.environ["FAKE_CALLS"]).open("a", encoding="utf-8") as calls:
        calls.write(json.dumps([tool, *args]) + "\n")


def _assets_json() -> str:
    """Render the release's assets as ``gh release view --json assets`` does."""
    assets = [
        {"name": path.name, "digest": f"sha256:{_digest(path)}"}
        for path in sorted(_directory("FAKE_GITHUB_ASSETS").iterdir())
    ]
    return json.dumps({"assets": assets})


def _gh_upload(files: list[str]) -> int:
    """Attach ``files``, refusing any name the release already holds."""
    assets = _directory("FAKE_GITHUB_ASSETS")
    if "--clobber" in files:
        print("the fake release refuses --clobber", file=sys.stderr)
        return 1
    for name in files:
        target = assets / Path(name).name
        if target.exists():
            print(f"asset {target.name} already exists", file=sys.stderr)
            return 1
        shutil.copyfile(name, target)
    return 0


def _gh_download(args: list[str]) -> int:
    """Copy the asset ``--pattern`` names into ``--dir``."""
    pattern = args[args.index("--pattern") + 1]
    target = Path(args[args.index("--dir") + 1])
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_directory("FAKE_GITHUB_ASSETS") / pattern, target / pattern)
    return 0


def gh(args: list[str]) -> int:
    """Serve the ``gh release`` calls the release workflow makes."""
    command = args[1] if len(args) > 1 else ""
    if command == "view":
        print(_assets_json())
        return 0
    if command == "upload":
        return _gh_upload(args[3:])
    if command == "download":
        return _gh_download(args)
    return 0


def _next_status() -> str:
    """Return the next scripted index status; the last one repeats."""
    statuses = os.environ.get("FAKE_CURL_STATUSES", "200").split(",")
    counter = Path(os.environ["FAKE_CALLS"]).with_suffix(".index-count")
    served = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(served + 1), encoding="utf-8")
    return statuses[min(served, len(statuses) - 1)]


def _index_json() -> str:
    """Render the PyPI state as a PEP 691 JSON simple index."""
    files = [
        {
            "filename": path.name,
            "url": f"{FILES_URL}{path.name}",
            "hashes": {"sha256": _digest(path)},
        }
        for path in sorted(_directory("FAKE_PYPI_FILES").iterdir())
    ]
    return json.dumps({"name": "cuprum", "files": files})


def curl(args: list[str]) -> int:
    """Serve the index or one file, writing ``%{http_code}`` if asked."""
    url, output = args[-1], Path(args[args.index("--output") + 1])
    if url == _INDEX_URL:
        status = _next_status()
        output.write_text(_index_json() if status == "200" else "{}", encoding="utf-8")
        print(status, end="")
        # curl exits non-zero, still writing `000`, when nothing answered.
        return 7 if status == "000" else 0
    source = _directory("FAKE_PYPI_FILES") / url.removeprefix(FILES_URL)
    if not source.exists():
        return 22
    shutil.copyfile(source, output)
    return 0


def main(argv: list[str]) -> int:
    """Dispatch to the stand-in named by the first argument."""
    tool, args = argv[0], argv[1:]
    _record(tool, args)
    return gh(args) if tool == "gh" else curl(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
