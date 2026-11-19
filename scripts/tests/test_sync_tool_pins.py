"""Contract tests for ``scripts.sync_tool_pins``.

The command's whole job is to make a small, deterministic edit, so these tests
drive it against a scratch copy of the six pin-bearing files rather than
asserting on its source text. Each case advances only the canonical
``pyproject.toml`` pin and then checks what the command reports and writes.
"""

from __future__ import annotations

import io
import typing as typ
from pathlib import Path

import pytest

from scripts import sync_tool_pins, tool_pins

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The six files whose pins the command reads or derives.
_PIN_FILES = (
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    ".github/workflows/ci.yml",
    ".github/workflows/build-wheels.yml",
    ".github/actions/build-wheels/action.yml",
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Return a scratch repository holding copies of the pin-bearing files."""
    for relative in _PIN_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((_REPO_ROOT / relative).read_bytes())
    return tmp_path


def _text(repository: Path, relative: str) -> str:
    """Return one fixture file's decoded text."""
    return (repository / relative).read_text(encoding="utf-8")


def _replace(repository: Path, relative: str, old: str, new: str) -> None:
    """Replace one unique occurrence in a fixture file."""
    text = _text(repository, relative)
    assert text.count(old) == 1, f"{relative} must hold exactly one {old!r}"
    (repository / relative).write_text(text.replace(old, new), encoding="utf-8")


def _bump(
    repository: Path,
    tool: str,
    old: str,
    new: str,
    *,
    lock: bool = True,
) -> None:
    """Advance a canonical pin, and the lock entry unless asked not to."""
    _replace(repository, "pyproject.toml", f"{tool}=={old}", f"{tool}=={new}")
    if lock:
        _replace(
            repository,
            "uv.lock",
            f'name = "{tool}"\nversion = "{old}"',
            f'name = "{tool}"\nversion = "{new}"',
        )


def _snapshot(repository: Path) -> dict[str, bytes]:
    """Return every fixture file's bytes."""
    return {relative: (repository / relative).read_bytes() for relative in _PIN_FILES}


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
    _bump(repository, "maturin", "1.15.0", "1.16.0")
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
    _bump(repository, "maturin", "1.15.0", "1.16.0")
    before_lock = _text(repository, "uv.lock")

    changed, output = _sync(repository)
    assert {path.relative_to(repository).as_posix() for path in changed} == {
        ".github/workflows/build-wheels.yml",
        ".github/actions/build-wheels/action.yml",
    }, output
    assert 'MATURIN_VERSION: "1.16.0"' in _text(
        repository, ".github/workflows/build-wheels.yml"
    ), "the workflow env pin must follow the canonical bump"
    assert '    default: "1.16.0"' in _text(
        repository, ".github/actions/build-wheels/action.yml"
    ), "the action default must follow the canonical bump"
    assert _text(repository, "uv.lock") == before_lock, "uv.lock is uv-owned"

    after = _snapshot(repository)
    assert not _sync(repository)[0], "a second sync must change nothing"
    assert _snapshot(repository) == after, "a second sync must not rewrite any file"


def test_a_ruff_bump_rewrites_the_makefile_and_ci_pins(repository: Path) -> None:
    """Both Ruff selectors follow the canonical 0.16.9 pin."""
    _bump(repository, "ruff", "0.16.4", "0.16.9")
    status, output = _check(repository)
    assert status == 1, output
    assert "Makefile RUFF_VERSION" in output, (
        f"drift must name the Makefile site, got {output!r}"
    )
    assert "ci.yml RUFF_VERSION" in output, (
        f"drift must name the ci.yml site, got {output!r}"
    )

    _sync(repository)
    assert "RUFF_VERSION ?= 0.16.9" in _text(repository, "Makefile"), (
        "sync must rewrite the Makefile Ruff default"
    )
    assert "RUFF_VERSION: '0.16.9'" in _text(repository, ".github/workflows/ci.yml"), (
        "sync must rewrite the ci.yml Ruff pin"
    )
    assert "RUFF_VERSION ?= 0.16.4" not in _text(repository, "Makefile"), (
        "the old Ruff default must not survive in the Makefile"
    )


def test_a_ty_bump_rewrites_the_makefile_and_ci_pins(repository: Path) -> None:
    """Both ty selectors follow the canonical 0.0.84 pin."""
    _bump(repository, "ty", "0.0.74", "0.0.84")
    _sync(repository)
    assert "TY_VERSION ?= 0.0.84" in _text(repository, "Makefile"), (
        "sync must rewrite the Makefile ty default"
    )
    assert "TY_VERSION: '0.0.84'" in _text(repository, ".github/workflows/ci.yml"), (
        "sync must rewrite the ci.yml ty pin"
    )


def test_a_stale_lock_is_reported_for_uv_and_left_alone(repository: Path) -> None:
    """Lock drift is uv's work, not this command's."""
    _bump(repository, "ruff", "0.16.4", "0.16.9", lock=False)
    before_lock = _text(repository, "uv.lock")
    _, output = _sync(repository)
    assert "uv lock" in output, f"sync must direct lock drift to uv, got {output!r}"
    assert _text(repository, "uv.lock") == before_lock, "sync must leave uv.lock alone"

    status, check_output = _check(repository)
    assert status == 1, f"a stale lock must fail the check, got {check_output!r}"
    assert "uv.lock ruff" in check_output, (
        f"drift must name the lock entry, got {check_output!r}"
    )
    assert "uv lock" in check_output, (
        f"drift must direct the lock entry to uv, got {check_output!r}"
    )


def _remove_ruff_requirement(repository: Path) -> None:
    """Drop the canonical Ruff declaration."""
    _replace(repository, "pyproject.toml", '    "ruff==0.16.4",\n', "")


def _range_ruff_requirement(repository: Path) -> None:
    """Turn the canonical Ruff pin into a range."""
    _replace(repository, "pyproject.toml", '"ruff==0.16.4"', '"ruff>=0.16.4"')


def _suffix_ruff_requirement(repository: Path) -> None:
    """Append an environment marker to the canonical Ruff pin."""
    _replace(
        repository,
        "pyproject.toml",
        '"ruff==0.16.4"',
        "\"ruff==0.16.4; python_version < '3.14'\"",
    )


def _duplicate_ruff_requirement(repository: Path) -> None:
    """Declare Ruff a second time."""
    _replace(
        repository,
        "pyproject.toml",
        '    "ruff==0.16.4",\n',
        '    "ruff==0.16.4",\n    "ruff==0.16.4",\n',
    )


def _remove_makefile_declaration(repository: Path) -> None:
    """Drop the Makefile Ruff default."""
    _replace(repository, "Makefile", "RUFF_VERSION ?= 0.16.4\n", "")


def _duplicate_makefile_declaration(repository: Path) -> None:
    """Declare the Makefile ty default twice."""
    _replace(
        repository,
        "Makefile",
        "TY_VERSION ?= 0.0.74\n",
        "TY_VERSION ?= 0.0.74\nTY_VERSION ?= 0.0.74\n",
    )


def _unquoted_workflow_declaration(repository: Path) -> None:
    """Remove the quoting the workflow site depends on."""
    _replace(
        repository,
        ".github/workflows/ci.yml",
        "RUFF_VERSION: '0.16.4'",
        "RUFF_VERSION: 0.16.4",
    )


def _ambiguous_action_default(repository: Path) -> None:
    """Add a second version-shaped default to the composite action."""
    _replace(
        repository,
        ".github/actions/build-wheels/action.yml",
        '    default: "1.15.0"\n',
        '    default: "1.15.0"\n    default: "9.9.9"\n',
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_remove_ruff_requirement, "exactly one ruff requirement"),
        (_range_ruff_requirement, "exact 'ruff==X.Y.Z' release"),
        (_suffix_ruff_requirement, "exact 'ruff==X.Y.Z' release"),
        (_duplicate_ruff_requirement, "exactly one ruff requirement"),
        (_remove_makefile_declaration, "Makefile RUFF_VERSION"),
        (_duplicate_makefile_declaration, "exactly one Makefile TY_VERSION"),
        (_unquoted_workflow_declaration, "ci.yml RUFF_VERSION"),
        (_ambiguous_action_default, "maturin-version default"),
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


def test_a_failed_replace_keeps_the_original_bytes(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An atomic write that cannot replace leaves no partial or temp file."""
    _bump(repository, "maturin", "1.15.0", "1.16.0")
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
