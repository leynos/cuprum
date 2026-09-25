"""Exercise the pure-python-wheel action's "Build sdist and wheel" step.

Without ``--sdist`` or ``--wheel``, ``uv build`` builds the sdist first and
then the wheel from that sdist, so a plain ``uv build --out-dir`` proves both
artefacts are buildable. Reading the YAML cannot show that the invocation
really omits those flags, or that the upload step's glob really matches both
files it produces; a fake ``uv`` that records its argv and drops stand-in
artefacts in place lets both be checked directly.
"""

from __future__ import annotations

import os
import shlex
import typing as typ

from tests.helpers.workflow_steps import find_step, install_tool, run_bash, step_script

if typ.TYPE_CHECKING:
    from pathlib import Path

ACTION = ".github/actions/pure-python-wheel"
BUILD_STEP = "Build sdist and wheel"
UPLOAD_STEP = "Upload sdist and wheel artefact"


def _build_argv(tmp_path: Path, out_dir: Path, *, emit_sdist: bool) -> list[str]:
    """Run the build step with a fake ``uv`` and return its recorded argv."""
    tools = tmp_path / "tools"
    record = tmp_path / "uv-argv"
    artefacts = 'touch "$OUT_DIR/pure_python_wheel-1.0-py3-none-any.whl"\n'
    if emit_sdist:
        artefacts += 'touch "$OUT_DIR/pure_python_wheel-1.0.tar.gz"\n'
    install_tool(
        tools,
        "uv",
        f'echo "$@" > "{record}"\nmkdir -p "$OUT_DIR"\n{artefacts}',
    )
    script = step_script(action=ACTION, name=BUILD_STEP)
    result = run_bash(
        script,
        cwd=tmp_path,
        env={"OUT_DIR": str(out_dir), "PATH": f"{tools}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    return shlex.split(record.read_text(encoding="utf-8"))


def test_build_step_invokes_uv_build_without_wheel_or_sdist_flags(
    tmp_path: Path,
) -> None:
    """The build must ask ``uv`` for both artefacts, not just one."""
    out_dir = tmp_path / "dist"
    argv = _build_argv(tmp_path, out_dir, emit_sdist=True)

    assert argv[0] == "build", f"expected uv's first argument to be build, got {argv!r}"
    assert "--out-dir" in argv, f"uv must be given --out-dir, got {argv!r}"
    assert argv[argv.index("--out-dir") + 1] == str(out_dir), (
        f"--out-dir must be followed by {out_dir!s}, got {argv!r}"
    )
    assert "--wheel" not in argv, (
        f"--wheel must be omitted so the sdist also builds, got {argv!r}"
    )
    assert "--sdist" not in argv, (
        f"--sdist must be omitted so the wheel also builds, got {argv!r}"
    )


def _matched_upload_artefacts(tmp_path: Path, out_dir: Path) -> set[str]:
    """Return the file names the upload step's glob patterns would collect."""
    step = find_step(action=ACTION, name=UPLOAD_STEP)
    inputs = typ.cast("dict[str, object]", step.get("with"))
    declared_path = typ.cast("str", inputs["path"])
    return {
        match.name
        for line in declared_path.splitlines()
        if line.strip()
        for match in out_dir.glob(line.strip().replace("${{ inputs.out-dir }}/", ""))
    }


def test_upload_globs_match_both_sdist_and_wheel(tmp_path: Path) -> None:
    """A regression that stops emitting the sdist must fail this contract."""
    out_dir = tmp_path / "dist"
    _build_argv(tmp_path, out_dir, emit_sdist=True)

    matches = _matched_upload_artefacts(tmp_path, out_dir)
    assert any(name.endswith(".whl") for name in matches), (
        f"upload globs must match the built wheel, matched {matches!r}"
    )
    assert any(name.endswith(".tar.gz") for name in matches), (
        f"upload globs must match the built sdist, matched {matches!r}"
    )


def test_upload_globs_would_catch_a_missing_sdist(tmp_path: Path) -> None:
    """A fake ``uv`` that only emits a wheel must leave the sdist glob empty."""
    out_dir = tmp_path / "dist"
    _build_argv(tmp_path, out_dir, emit_sdist=False)

    matches = _matched_upload_artefacts(tmp_path, out_dir)
    assert any(name.endswith(".whl") for name in matches), (
        f"upload globs must still match the built wheel, matched {matches!r}"
    )
    assert not any(name.endswith(".tar.gz") for name in matches), (
        f"upload globs must not match a sdist that was never built, matched {matches!r}"
    )
