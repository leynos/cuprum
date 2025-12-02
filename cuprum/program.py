"""Represent curated executables with a nominal ``Program`` type.

Examples:
>>> from cuprum.program import Program
>>> ECHO = Program("echo")
>>> ECHO == "echo"
True

"""

from __future__ import annotations

import typing as typ

Program = typ.NewType("Program", str)

__all__ = ["Program"]
