"""Stable JSON artefact output for tee profiling."""

from __future__ import annotations

import json
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth


def _write_json(path: pth.Path, payload: cabc.Mapping[str, object]) -> None:
    """Write stable JSON output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
