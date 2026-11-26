"""Contract tests for ``scripts.sync_tool_pins``.

The command's whole job is to make a small, deterministic edit, so these tests
drive it against a scratch copy of the six pin-bearing files rather than
asserting on its source text. Each case advances only the canonical
``pyproject.toml`` pin and then checks what the command reports and writes.
The fixture edits and their versions live in :mod:`tool_pin_support`, which
derives them from the copied files, so a live pin bump does not leave a case
failing in its setup before it has exercised anything.
"""

from __future__ import annotations

import io
import typing as typ
from pathlib import Path

import pytest

from scripts import sync_tool_pins, tool_pins
from scripts.tests import tool_pin_support as pins

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Return a scratch repository holding copies of the pin-bearing files."""
    for relative in pins.PIN_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((_REPO_ROOT / relative).read_bytes())
    return tmp_path


def _snapshot(repository: Path) -> dict[str, bytes]:
    """Return every fixture file's bytes."""
    return {
        relative: (repository / relative).read_bytes() for relative in pins.PIN_FILES
    }


def _check(repository: Path) -> tuple[int, str]:
    """Run ``check`` quietly and return its status and output."""
    output = io.StringIO()
    status = sync_tool_pins.run_check(repository, stream=output)
    return status, output.getvalue()


def _sync(repository: Path) -> tuple[tuple[Path, ...], str]:
    """Run ``sync`` quietly and return its changed files and output."""
    output = io.StringIO()
    changed = sync_tool_pins.sync_pins(repository, stream=output)
    return changed, output.getvalue()


def test_check_accepts_a_synchronized_repository(repository: Path) -> None:
    """An unchanged checkout reports no drift."""
    status, output = _check(repository)
    assert status == 0, output
    assert "match" in output, (
        f"a synchronized repository must report a match, got {output!r}"
    )


def test_check_never_writes(repository: Path) -> None:
    """Reporting drift leaves every byte in place."""
    pins.bump(repository, "maturin")
    before = _snapshot(repository)
    status, output = _check(repository)
    assert status == 1, "a stale derived input must fail the check"
    assert "MATURIN_VERSION" in output, (
        f"drift must name the stale site, got {output!r}"
    )
    assert _snapshot(repository) == before, "check must not write any file"


def test_sync_is_a_no_op_when_pins_are_synchronized(repository: Path) -> None:
    """Synchronized inputs produce no writes, so the command is idempotent."""
    before = _snapshot(repository)
    changed, _ = _sync(repository)
    assert not changed, (
        f"a synchronized repository must produce no writes, got {changed}"
    )
    assert _snapshot(repository) == before, "a no-op sync must not rewrite any file"


def test_a_maturin_bump_rewrites_both_derived_literals(repository: Path) -> None:
    """The workflow env pin and action default follow the canonical version."""
    _, new = pins.bump(repository, "maturin")
    before_lock = pins.text(repository, "uv.lock")

    changed, output = _sync(repository)
    assert {path.relative_to(repository).as_posix() for path in changed} == {
        ".github/workflows/build-wheels.yml",
        ".github/actions/build-wheels/action.yml",
    }, output
    assert f'MATURIN_VERSION: "{new}"' in pins.text(
        repository, ".github/workflows/build-wheels.yml"
    ), "the workflow env pin must follow the canonical bump"
    assert f'    default: "{new}"' in pins.text(
        repository, ".github/actions/build-wheels/action.yml"
    ), "the action default must follow the canonical bump"
    assert pins.text(repository, "uv.lock") == before_lock, "uv.lock is uv-owned"

    after = _snapshot(repository)
    assert not _sync(repository)[0], "a second sync must change nothing"
    assert _snapshot(repository) == after, "a second sync must not rewrite any file"


def test_a_ruff_bump_rewrites_the_makefile_and_ci_pins(repository: Path) -> None:
    """Both Ruff selectors follow the canonical bump."""
    old, new = pins.bump(repository, "ruff")
    status, output = _check(repository)
    assert status == 1, output
    assert "Makefile RUFF_VERSION" in output, (
        f"drift must name the Makefile site, got {output!r}"
    )
    assert "ci.yml RUFF_VERSION" in output, (
        f"drift must name the ci.yml site, got {output!r}"
    )

    _sync(repository)
    assert f"RUFF_VERSION ?= {new}" in pins.text(repository, "Makefile"), (
        "sync must rewrite the Makefile Ruff default"
    )
    assert f"RUFF_VERSION: '{new}'" in pins.text(
        repository, ".github/workflows/ci.yml"
    ), "sync must rewrite the ci.yml Ruff pin"
    assert f"RUFF_VERSION ?= {old}" not in pins.text(repository, "Makefile"), (
        "the old Ruff default must not survive in the Makefile"
    )


def test_a_commented_makefile_default_is_still_synchronized(
    repository: Path,
) -> None:
    """A trailing comment is preserved while the value is rewritten."""
    old = pins.pin(repository, "ruff")
    new = pins.next_patch(old)
    pins.replace(
        repository,
        "Makefile",
        f"RUFF_VERSION ?= {old}\n",
        f"RUFF_VERSION ?= {old}  # keep in step with the dev group\n",
    )
    status, output = _check(repository)
    assert status == 0, f"a commented default must still match, got {output!r}"

    pins.bump(repository, "ruff")
    status, output = _check(repository)
    assert status == 1, output
    assert "Makefile RUFF_VERSION" in output, (
        f"drift must name the commented site, got {output!r}"
    )

    _sync(repository)
    assert f"RUFF_VERSION ?= {new}  # keep in step with the dev group" in pins.text(
        repository, "Makefile"
    ), "sync must rewrite the value and keep the comment"


def test_an_empty_makefile_default_is_drift_and_is_repaired(repository: Path) -> None:
    """Make defines the variable on an empty ``?=``, so it must not be skipped."""
    pins.empty_makefile_declaration(repository)
    status, output = _check(repository)
    assert status == 1, output
    assert "Makefile RUFF_VERSION" in output, (
        f"an empty default must be reported as drift, got {output!r}"
    )

    _sync(repository)
    status, output = _check(repository)
    assert status == 0, f"sync must repair the empty default, got {output!r}"


def test_a_ty_bump_rewrites_the_makefile_and_ci_pins(repository: Path) -> None:
    """Both ty selectors follow the canonical bump."""
    _, new = pins.bump(repository, "ty")
    _sync(repository)
    assert f"TY_VERSION ?= {new}" in pins.text(repository, "Makefile"), (
        "sync must rewrite the Makefile ty default"
    )
    assert f"TY_VERSION: '{new}'" in pins.text(
        repository, ".github/workflows/ci.yml"
    ), "sync must rewrite the ci.yml ty pin"


def test_a_stale_lock_is_reported_for_uv_and_left_alone(repository: Path) -> None:
    """Lock drift is uv's work, not this command's."""
    pins.bump(repository, "ruff", lock=False)
    before_lock = pins.text(repository, "uv.lock")
    _, output = _sync(repository)
    assert "uv lock" in output, f"sync must direct lock drift to uv, got {output!r}"
    assert pins.text(repository, "uv.lock") == before_lock, (
        "sync must leave uv.lock alone"
    )

    status, check_output = _check(repository)
    assert status == 1, f"a stale lock must fail the check, got {check_output!r}"
    assert "uv.lock ruff" in check_output, (
        f"drift must name the lock entry, got {check_output!r}"
    )
    assert "uv lock" in check_output, (
        f"drift must direct the lock entry to uv, got {check_output!r}"
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (pins.remove_ruff_requirement, "exactly one ruff requirement"),
        (pins.range_ruff_requirement, "exact 'ruff==X.Y.Z' release"),
        (pins.suffix_ruff_requirement, "exact 'ruff==X.Y.Z' release"),
        (pins.duplicate_ruff_requirement, "exactly one ruff requirement"),
        (pins.remove_makefile_declaration, "Makefile RUFF_VERSION"),
        (pins.duplicate_makefile_declaration, "exactly one Makefile TY_VERSION"),
        (pins.empty_leading_makefile_declaration, "exactly one Makefile RUFF_VERSION"),
        (
            pins.indented_leading_makefile_declaration,
            "exactly one Makefile RUFF_VERSION",
        ),
        (
            pins.malformed_leading_makefile_declaration,
            "exactly one Makefile RUFF_VERSION",
        ),
        (pins.multi_token_makefile_declaration, "must be one token"),
        (pins.unquoted_workflow_declaration, "ci.yml RUFF_VERSION"),
        (pins.ambiguous_action_default, "maturin-version default"),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_missing_malformed_or_ambiguous_pins_fail_clearly(
    repository: Path,
    mutate: cabc.Callable[[Path], None],
    message: str,
) -> None:
    """Every structural defect is named rather than silently accepted."""
    mutate(repository)
    with pytest.raises(tool_pins.PinError, match=message):
        tool_pins.check_pins(repository)
    with pytest.raises(tool_pins.PinError, match=message):
        sync_tool_pins.sync_pins(repository)


def test_a_late_structural_defect_writes_no_file(repository: Path) -> None:
    """A defect found after an earlier drift leaves the earlier file untouched.

    The sites are matched file by file, so a malformed site in a later file
    would otherwise be discovered only after the earlier files had been
    rewritten, leaving edits behind on a command that reported failure.
    """
    pins.bump(repository, "ruff")
    pins.ambiguous_action_default(repository)
    before = _snapshot(repository)

    with pytest.raises(tool_pins.PinError, match="maturin-version default"):
        sync_tool_pins.sync_pins(repository)

    assert _snapshot(repository) == before, (
        "a validation failure must not leave any file rewritten"
    )


def test_a_crlf_checkout_is_already_synchronized(repository: Path) -> None:
    """Git's `core.autocrlf` on Windows must not turn a valid pin into drift."""
    pins.to_crlf(repository)
    status, output = _check(repository)
    assert status == 0, f"a CRLF checkout must report a match, got {output!r}"


def test_a_crlf_checkout_is_repaired_with_its_terminators_intact(
    repository: Path,
) -> None:
    """Sync rewrites the value and leaves every CRLF line ending alone."""
    pins.to_crlf(repository)
    _, new = pins.bump(repository, "ruff")
    _sync(repository)

    body = (repository / "Makefile").read_bytes()
    assert f"RUFF_VERSION ?= {new}\r\n".encode() in body, (
        "sync must rewrite the value and keep the CRLF terminator"
    )
    assert b"\n" not in body.replace(b"\r\n", b""), (
        "sync must not introduce a bare LF into a CRLF checkout"
    )


def test_sync_keeps_a_rewritten_file_executable(repository: Path) -> None:
    """A mode the file already carries must survive the atomic replace."""
    target = repository / "Makefile"
    target.chmod(0o754)
    before = target.stat().st_mode & 0o777
    assert before == 0o754, f"the fixture mode must be 0o754, found {before:o}"

    pins.bump(repository, "ruff")
    _sync(repository)

    after = target.stat().st_mode & 0o777
    assert after == 0o754, (
        f"sync must preserve the file's mode, {before:o} became {after:o}"
    )


def test_a_failed_replace_keeps_the_original_bytes(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An atomic write that cannot replace leaves no partial or temp file."""
    pins.bump(repository, "maturin")
    before = _snapshot(repository)

    def _fail_replace(_self: Path, _destination: object) -> None:
        msg = "simulated disk failure"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", _fail_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        sync_tool_pins.sync_pins(repository)

    assert _snapshot(repository) == before, "a failed write must not corrupt files"
    leftovers = [
        path
        for path in repository.rglob("*")
        if path.is_file() and path.name.startswith(".")
    ]
    assert leftovers == [], f"temporary files survived a failed write: {leftovers}"
