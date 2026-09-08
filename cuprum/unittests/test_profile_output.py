"""Tests for stable tee-profile JSON artefact output."""

from __future__ import annotations

import json
import typing as typ

from benchmarks.tee_profile_output import _write_json

if typ.TYPE_CHECKING:
    import pathlib as pth


def test_write_json_creates_parent_and_writes_stable_payload(
    tmp_path: pth.Path,
) -> None:
    """Nested artefacts have sorted, indented JSON with one final newline."""
    path = tmp_path / "nested" / "profile" / "result.json"
    payload = {"z": {"enabled": True}, "a": 1}

    _write_json(path, payload)

    text = path.read_text()
    assert path.is_file(), "writer must create each missing parent directory"
    assert json.loads(text) == payload, "writer must preserve the JSON payload"
    assert text == '{\n  "a": 1,\n  "z": {\n    "enabled": true\n  }\n}\n', (
        "writer must sort keys, use two-space indentation, and end with one newline"
    )
