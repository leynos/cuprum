"""Execute the install-makeutil action's step against a stand-in download.

`tests/test_ci_makeutil_install.py` holds the step's text. These tests run it:
the step's own ``run`` body executes under ``bash`` with ``curl`` replaced by a
command that records its arguments and writes chosen bytes, while
``sha256sum`` and ``install`` are the real ones. That shows what the text
cannot: that the pinned release URL is fetched, that a binary whose digest
differs from the pin is refused and never installed, and that a failed
download fails the step.

Nothing touches the network.
"""

from __future__ import annotations

import hashlib
import os
import typing as typ

from tests.helpers.composite_actions import action_document, run_step, step_script

if typ.TYPE_CHECKING:
    from pathlib import Path

ACTION: typ.Final = ".github/actions/install-makeutil"
STEP: typ.Final = "Download the pinned makeutil release"
#: The stand-in `curl`: it logs its arguments, then writes `PAYLOAD` to the
#: path after `--output`, or fails when `FAIL` is set.
STAND_IN_CURL: typ.Final = """#!/bin/bash
printf '%s\\n' "$*" >> "${CALL_LOG}"
if [ -n "${FAIL:-}" ]; then
  exit 22
fi
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then
    printf '%s' "${PAYLOAD}" > "$2"
  fi
  shift
done
"""
#: Bytes the stand-in serves as the binary.
PAYLOAD: typ.Final = "#!/bin/sh\necho makeutil\n"


def _pin() -> dict[str, str]:
    """Return the step's declared ``env``, which holds the pin."""
    runs = typ.cast("dict[str, object]", action_document(ACTION)["runs"])
    step = typ.cast("list[dict[str, object]]", runs["steps"])[0]
    return typ.cast("dict[str, str]", step["env"])


def _run(tmp_path: Path, *, digest: str, fail: bool = False) -> tuple[int, str]:
    """Run the step with the stand-in `curl` and ``digest`` as the pin."""
    commands = tmp_path / "commands"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text(STAND_IN_CURL, encoding="utf-8")
    curl.chmod(0o755)
    log = tmp_path / "calls"
    log.touch()
    result = run_step(
        step_script(ACTION, STEP),
        workdir=tmp_path,
        environment={
            "PATH": f"{commands}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "PAYLOAD": PAYLOAD,
            "FAIL": "1" if fail else "",
            **_pin(),
            "MAKEUTIL_SHA256": digest,
        },
    )
    return result.returncode, log.read_text(encoding="utf-8")


def _installed(tmp_path: Path) -> Path:
    """Return where the step installs makeutil under the test's ``HOME``."""
    return tmp_path / ".cargo" / "bin" / "makeutil"


def test_a_matching_digest_installs_the_pinned_release(tmp_path: Path) -> None:
    """The pinned URL is fetched and the checked binary installed executable."""
    digest = hashlib.sha256(PAYLOAD.encode()).hexdigest()
    status, calls = _run(tmp_path, digest=digest)
    pin = _pin()
    assert status == 0, calls
    expected = (
        "https://github.com/leynos/makeutil/releases/download/"
        f"{pin['MAKEUTIL_VERSION']}/makeutil-{pin['MAKEUTIL_TARGET']}"
    )
    assert expected in calls, calls
    binary = _installed(tmp_path)
    assert binary.read_text(encoding="utf-8") == PAYLOAD, (
        "the installed makeutil must be the checked download"
    )
    assert os.access(binary, os.X_OK), "the installed makeutil must be executable"


def test_a_digest_mismatch_fails_and_installs_nothing(tmp_path: Path) -> None:
    """A binary that is not the pinned one must never reach ``PATH``."""
    status, _ = _run(tmp_path, digest="0" * 64)
    assert status != 0, "a digest mismatch must fail the step"
    assert not _installed(tmp_path).exists(), "a refused binary must not be installed"


def test_a_failed_download_fails_and_installs_nothing(tmp_path: Path) -> None:
    """A download error stops the step before the check and the install."""
    digest = hashlib.sha256(PAYLOAD.encode()).hexdigest()
    status, _ = _run(tmp_path, digest=digest, fail=True)
    assert status != 0, "a failed download must fail the step"
    assert not _installed(tmp_path).exists(), "nothing may be installed"
