"""Shared schema helpers and findings for the nose duplication gate.

This module owns the gate's error vocabulary, the gate-neutral finding
types, and the validation of the ``nose query --format json`` report.
``scripts/nose_detector.py`` drives the binary itself, and
``scripts/duplication_gate.py`` owns the reasoned allowlist and CLI.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b`` (``scripts/nose_schema.py``),
the merged revision of PR #276, under the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from collections import abc as cabc
from pathlib import Path


class GateConfigError(ValueError):
    """Raised when the gate configuration or the detector report is malformed."""


class GateExecutionError(GateConfigError):
    """Raised when the pinned detector is missing, wrong, or fails to run."""


@dc.dataclass(frozen=True, slots=True)
class Location:
    """One duplicated region reported by nose.

    Attributes
    ----------
    file : str
        Repository-relative POSIX path of the duplicated region.
    start, end : int
        Inclusive first and last source line of the region.
    name : str | None
        Unit name when nose matched a whole function, class, or method;
        ``None`` for fragment-level matches such as import blocks.
    """

    file: str
    start: int
    end: int
    name: str | None

    @property
    def span(self) -> str:
        """The ``path:start-end`` span of this location."""
        return f"{self.file}:{self.start}-{self.end}"

    @property
    def label(self) -> str:
        """The span, suffixed with the unit name when nose named one."""
        return self.span if self.name is None else f"{self.span} {self.name}"


@dc.dataclass(frozen=True, slots=True)
class Finding:
    """One duplication family in gate-neutral form.

    Attributes
    ----------
    witness : str
        nose evidence kind (``exact``, ``copy-paste``, ``similar``, ...).
    value : float
        nose refactoring value; the gate reports it and orders by it.
    locations : tuple[Location, ...]
        Every duplicated region in the family, in report order.
    """

    witness: str
    value: float
    locations: tuple[Location, ...]

    @property
    def label(self) -> str:
        """The ``path:lines ~ path:lines`` summary of the family."""
        return " ~ ".join(location.label for location in self.locations)


def require_table(value: object, *, context: str) -> cabc.Mapping[str, object]:
    """Validate one TOML table before configuration logic consumes it.

    Parameters
    ----------
    value : object
        Candidate TOML value.
    context : str
        Configuration path used in an invalid-value diagnostic.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The validated table with string keys.

    Raises
    ------
    GateConfigError
        If ``value`` is not a table with string keys.
    """
    if not isinstance(value, cabc.Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        msg = f"{context} must be a table with string keys"
        raise GateConfigError(msg)
    return typ.cast("cabc.Mapping[str, object]", value)


def require_string(value: object, *, context: str) -> str:
    """Validate one required non-empty configuration string."""
    return _require(
        value,
        predicate=_is_non_empty_string,
        requirement="a non-empty string",
        context=context,
    )


def require_string_tuple(value: object, *, context: str) -> tuple[str, ...]:
    """Validate one configuration array of non-empty strings.

    Parameters
    ----------
    value : object
        Candidate configuration value.
    context : str
        Configuration path used in an invalid-value diagnostic.

    Returns
    -------
    tuple[str, ...]
        The validated configuration strings in input order.

    Raises
    ------
    GateConfigError
        If ``value`` is not an array of non-empty strings.
    """
    if not isinstance(value, cabc.Sequence) or isinstance(value, (str, bytes)):
        msg = f"{context} must be an array of strings"
        raise GateConfigError(msg)
    return tuple(require_string(item, context=f"{context}[]") for item in value)


def _is_non_empty_string(value: object) -> typ.TypeIs[str]:
    """Report whether ``value`` is a non-empty string."""
    return isinstance(value, str) and bool(value)


def _is_integer(value: object) -> typ.TypeIs[int]:
    """Report whether ``value`` is an integer rather than a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require[T](
    value: object,
    *,
    predicate: cabc.Callable[[object], typ.TypeIs[T]],
    requirement: str,
    context: str,
) -> T:
    """Return *value* as *T*, or refuse it with the unmet *requirement*.

    Both :func:`require_string` and :func:`require_positive_int` are this
    shape: test one typed predicate and, when it holds, hand the caller back
    the same object with the predicate's type. Only the predicate, the
    requirement named in the diagnostic, and the calling validator's own
    context differ, so the guard lives here once rather than being retyped per
    validator. The ``TypeIs`` predicate is what makes the narrowed return sound
    to a type checker; a plain ``bool`` predicate would leave *value* as
    ``object``.

    Parameters
    ----------
    value : object
        Candidate configuration value.
    predicate : collections.abc.Callable[[object], typing.TypeIs[T]]
        Typed test the value must satisfy.
    requirement : str
        Requirement phrase completing the ``must be ...`` diagnostic.
    context : str
        Configuration path used in an invalid-value diagnostic.

    Returns
    -------
    T
        *value*, narrowed to the predicate's type.

    Raises
    ------
    GateConfigError
        If *value* does not satisfy *predicate*.
    """
    if not predicate(value):
        msg = f"{context} must be {requirement}"
        raise GateConfigError(msg)
    return value


def _is_positive_integer(value: object) -> typ.TypeIs[int]:
    """Report whether ``value`` is an integer of at least one."""
    return _is_integer(value) and value >= 1


def require_positive_int(value: object, *, context: str) -> int:
    """Validate one required positive configuration integer."""
    return _require(
        value,
        predicate=_is_positive_integer,
        requirement="a positive integer",
        context=context,
    )


def normalize_findings(report: object) -> list[Finding]:
    """Convert one validated nose report into ordered gate findings.

    Parameters
    ----------
    report : object
        Decoded ``nose query --format json`` report.

    Returns
    -------
    list[Finding]
        Findings ordered by descending value, then by source location.

    Raises
    ------
    GateConfigError
        If the report does not match the expected schema.
    """
    families = require_table(report, context="nose report").get("families")
    if not isinstance(families, cabc.Sequence) or isinstance(families, (str, bytes)):
        msg = "nose report families must be an array"
        raise GateConfigError(msg)
    findings = [
        _finding(family, context=f"nose report families[{index}]")
        for index, family in enumerate(families)
    ]
    findings.sort(key=lambda finding: (-finding.value, finding.label))
    return findings


def _finding_value(value: object, *, context: str) -> float:
    """Validate a family's refactoring value as a non-boolean number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{context}.value must be a number"
        raise GateConfigError(msg)
    return float(value)


def _finding_locations(value: object, *, context: str) -> tuple[Location, ...]:
    """Validate a family's member locations, preserving their report order."""
    if not isinstance(value, cabc.Sequence) or isinstance(value, (str, bytes)):
        msg = f"{context}.locations must be an array"
        raise GateConfigError(msg)
    if not value:
        msg = f"{context}.locations must not be empty"
        raise GateConfigError(msg)
    return tuple(
        _location(location, context=f"{context}.locations[{index}]")
        for index, location in enumerate(value)
    )


def _finding(raw: object, *, context: str) -> Finding:
    """Validate one nose family payload."""
    family = require_table(raw, context=context)
    return Finding(
        witness=require_string(family.get("witness"), context=f"{context}.witness"),
        value=_finding_value(family.get("value"), context=context),
        locations=_finding_locations(family.get("locations"), context=context),
    )


def _location(raw: object, *, context: str) -> Location:
    """Validate one nose location payload."""
    location = require_table(raw, context=context)
    start = require_positive_int(location.get("start"), context=f"{context}.start")
    end = location.get("end")
    if not _is_integer(end) or end < start:
        msg = f"{context}.end must not precede start"
        raise GateConfigError(msg)
    raw_name = location.get("name")
    name: str | None = None
    if raw_name is not None:
        if not _is_non_empty_string(raw_name):
            msg = f"{context}.name must be a non-empty string or null"
            raise GateConfigError(msg)
        name = raw_name
    return Location(
        file=Path(
            require_string(location.get("file"), context=f"{context}.file")
        ).as_posix(),
        start=start,
        end=end,
        name=name,
    )
