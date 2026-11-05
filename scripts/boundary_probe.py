"""Run byte-safe unsafe-code probes against isolated Cargo targets."""

from __future__ import annotations

import base64
import contextlib
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

UNSAFE_PROBES = (
    "pub fn unsafe_probe() { unsafe {} }",
    "pub unsafe fn unsafe_probe() {}",
    "unsafe trait Probe {} unsafe impl Probe for () {}",
)


def unsafe_was_forbidden(code: int, output: str) -> bool:
    """Return whether compiler output shows the required unsafe prohibition.

    Parameters
    ----------
    code : int
        Compiler process exit code.
    output : str
        Combined compiler diagnostic text.

    Returns
    -------
    bool
        Whether compilation failed and its output names a required unsafe
        prohibition marker.
    """
    markers = ("forbid(unsafe_code)", "-F unsafe-code")
    return code != 0 and any(marker in output for marker in markers)


@contextlib.contextmanager
def probe_appended(path: Path, probe: str) -> cabc.Iterator[None]:
    """Append a probe temporarily and restore the source bytes on context exit.

    The original bytes are written back from a ``finally`` block even when
    probe compilation or assertions fail. A failure while restoring the file
    propagates to the caller.

    Parameters
    ----------
    path : Path
        Source file receiving the temporary probe.
    probe : str
        UTF-8 text appended after the original bytes.

    Yields
    ------
    None
        Control to the caller while the probe is appended.

    Raises
    ------
    OSError
        If the source cannot be read, written, or restored.
    """  # ruff: ignore[docstring-extraneous-exception] - filesystem errors propagate from Path reads and writes.
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n" + probe.encode() + b"\n")
        yield
    finally:
        path.write_bytes(original)


def probe_log_name(workspace: Path, path: Path, index: int) -> str:
    """Return a filesystem-safe name encoding one workspace-relative source.

    Parameters
    ----------
    workspace : Path
        Workspace root used to make the source path relative.
    path : Path
        Source file associated with the probe log.
    index : int
        Zero-based probe index included in the log name.

    Returns
    -------
    str
        Reversible, URL-safe log name containing the source path and index.

    Raises
    ------
    ValueError
        If ``path`` is not beneath ``workspace``.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from Path.relative_to.
    relative = path.relative_to(workspace).as_posix()
    encoded = base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")
    return f"{path.stem}-{encoded}-unsafe-{index}.log"


@dc.dataclass(frozen=True, slots=True)
class _ProbeConfiguration:
    """Bundle probe source and compiler callback for one audited target."""

    probes: tuple[str, ...]
    compile_workspace: cabc.Callable[[Path], tuple[int, str]]


def check_target(
    *,
    workspace: Path,
    path: Path,
    logs: Path,
    configuration: _ProbeConfiguration,
) -> None:
    """Compile each temporary probe and archive its diagnostic output.

    Parameters
    ----------
    workspace : Path
        Copied Cargo workspace passed to the configured compiler callback.
    path : Path
        Audited source file receiving each temporary unsafe-code probe.
    logs : Path
        Directory receiving one diagnostic log for each probe.
    configuration : _ProbeConfiguration
        Probe sources and callback that compiles the workspace.

    Raises
    ------
    RuntimeError
        If compilation does not reject a probe with the required unsafe
        prohibition diagnostic.
    OSError
        If the source file or a diagnostic log cannot be read or written.
    Exception
        If the configured compiler callback raises; the exception propagates
        after the original source bytes are restored.
    """  # ruff: ignore[docstring-extraneous-exception] - callback and filesystem errors propagate through the probe context.
    for index, probe in enumerate(configuration.probes):
        with probe_appended(path, probe):
            code, output = configuration.compile_workspace(workspace)
            name = probe_log_name(workspace, path, index)
            (logs / name).write_text(output, encoding="utf-8")
            if not unsafe_was_forbidden(code, output):
                msg = f"safe target did not reject unsafe probe: {name}"
                raise RuntimeError(msg)
