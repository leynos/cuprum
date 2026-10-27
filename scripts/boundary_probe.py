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
    """Return whether a failed compile names the required unsafe prohibition."""
    markers = ("forbid(unsafe_code)", "-F unsafe-code")
    return code != 0 and any(marker in output for marker in markers)


@contextlib.contextmanager
def probe_appended(path: Path, probe: str) -> cabc.Iterator[None]:
    """Temporarily append a byte-preserving probe, then restore the source."""
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n" + probe.encode() + b"\n")
        yield
    finally:
        path.write_bytes(original)


def probe_log_name(workspace: Path, path: Path, index: int) -> str:
    """Return a filesystem-safe, reversible workspace-relative probe log name."""
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
    """Compile each temporary probe and archive its corresponding diagnostic."""
    for index, probe in enumerate(configuration.probes):
        with probe_appended(path, probe):
            code, output = configuration.compile_workspace(workspace)
            name = probe_log_name(workspace, path, index)
            (logs / name).write_text(output, encoding="utf-8")
            if not unsafe_was_forbidden(code, output):
                msg = f"safe target did not reject unsafe probe: {name}"
                raise RuntimeError(msg)
