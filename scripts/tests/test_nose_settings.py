"""`[tool.nose]` settings parsing for the duplication gate.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/tests/test_nose_detector.py``), the merged revision of PR #276,
under the ISC terms in ``LICENSE``.

The settings table is the gate's only source of scan scope, so a mistyped or
missing key must fail loudly rather than silently narrowing the scan to
nothing.
"""

from __future__ import annotations

import re
import typing as typ

import pytest

from scripts.tests.duplication_gate_test_support import detector
from scripts.tests.nose_detector_test_support import settings_body, write_settings

if typ.TYPE_CHECKING:
    from pathlib import Path


class TestLoadSettings:
    """`[tool.nose]` settings parsing."""

    def test_loads_the_repository_settings(self, tmp_path: Path) -> None:
        """A complete table produces validated settings."""
        pyproject = write_settings(
            tmp_path,
            """\
            [tool.nose]
            version = "0.20.0"
            roots = ["cuprum"]
            mode = "syntax"
            min-size = 24
            surface = "all"
            top = 30
            exclude = ["**/generated/**"]
            """,
        )
        settings = detector.load_settings(pyproject)
        assert settings.roots == ("cuprum",), "Roots must round-trip in order."
        assert settings.exclude == ("**/generated/**",), (
            "Exclude globs must round-trip."
        )
        assert settings.top == 30, "The ranking bound must round-trip."

    def test_top_and_exclude_are_optional(self, tmp_path: Path) -> None:
        """Omitted optional keys fall back to nose's own view size."""
        pyproject = write_settings(
            tmp_path,
            """\
            [tool.nose]
            version = "0.20.0"
            roots = ["cuprum"]
            mode = "syntax"
            min-size = 24
            """,
        )
        settings = detector.load_settings(pyproject)
        assert settings.top is None, "An omitted `top` must not bound the view."
        assert settings.surface == "all", "The gate defaults to the widened surface."

    @pytest.mark.parametrize(
        ("body", "diagnostic"),
        [
            (
                settings_body(version=None),
                "tool.nose.version must be a non-empty string",
            ),
            (
                settings_body(roots='"cuprum"'),
                "tool.nose.roots must be an array of strings",
            ),
            (
                settings_body(min_size="0"),
                "tool.nose.min-size must be a positive integer",
            ),
            (
                settings_body(surface='"everything"'),
                "tool.nose.surface must be 'default' or 'all'",
            ),
        ],
        ids=["missing-version", "string-roots", "zero-min-size", "bad-surface"],
    )
    def test_rejects_malformed_settings(
        self, tmp_path: Path, body: str, diagnostic: str
    ) -> None:
        """Malformed settings raise a configuration error."""
        pyproject = write_settings(tmp_path, body)
        with pytest.raises(detector.GateConfigError, match=re.escape(diagnostic)):
            detector.load_settings(pyproject)
