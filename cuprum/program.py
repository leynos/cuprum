"""Program NewType representing curated executables."""

from __future__ import annotations

import typing as typ

Program = typ.NewType("Program", str)

__all__ = ["Program"]
