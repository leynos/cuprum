"""Shared fixtures for the nose-detector wrapper tests.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_nose_detector.py``), the merged revision of PR #276,
under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import textwrap
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path


def settings_body(
    *,
    version: str | None = '"0.20.0"',
    roots: str = '["cuprum"]',
    min_size: str = "24",
    surface: str | None = None,
) -> str:
    """Build a `[tool.nose]` table body from the supplied literal values."""
    lines = ["[tool.nose]"]
    if version is not None:
        lines.append(f"version = {version}")
    lines.extend((f"roots = {roots}", 'mode = "syntax"', f"min-size = {min_size}"))
    if surface is not None:
        lines.append(f"surface = {surface}")
    return "\n".join(lines) + "\n"


def write_settings(tmp_path: Path, body: str) -> Path:
    """Write a dedented `[tool.nose]` body to a manifest under ``tmp_path``."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(textwrap.dedent(body), encoding="utf-8")
    return pyproject
