"""Reasoned allowlist for the code-duplication gate.

Allow entries name locations by key rather than by line span, because spans
churn whenever code above them moves. A key is ``path`` (a glob matched
against the repository-relative path) optionally suffixed with ``::name`` to
require nose's unit name as well; ``::name`` keys never match the
fragment-level findings that nose reports without a name. An entry names
either one key (``unit = "..."``) or several (``members = ["...", ...]``),
and silences a family only when *every* location in that family matches one
of its keys, so a new copy in an unlisted file still blocks the gate. Every
entry records a reason so exceptions stay reviewable in version control.

Ported from ``leynos/episodic`` at
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``
(``scripts/duplication_allowlist.py``), the merged revision of PR #276, under
the ISC terms in ``LICENSE``.
"""

from __future__ import annotations

import dataclasses as dc
import fcntl
import tomllib
import typing as typ
from collections import abc as cabc
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import tomlkit
import tomlkit.exceptions
import tomlkit.items

from scripts.atomic_write import AtomicWriteOptions, atomic_write
from scripts.nose_schema import Finding, GateConfigError, Location

_MINIMUM_MEMBER_COUNT = 2


@dc.dataclass(frozen=True, slots=True)
class AllowEntry:
    """One reasoned exception from the duplication gate.

    Attributes
    ----------
    keys : tuple[str, ...]
        Location keys covered by this entry. Each key is ``path`` or
        ``path::name``, where ``path`` may use glob wildcards.
    reason : str
        Reviewable justification recorded alongside the entry.
    """

    keys: tuple[str, ...]
    reason: str

    def matches(self, finding: Finding) -> bool:
        """Report whether this entry covers every location in ``finding``."""
        return all(
            any(key_matches(key, location) for key in self.keys)
            for location in finding.locations
        )


def key_matches(key: str, location: Location) -> bool:
    """Report whether one allow key covers one reported location.

    Parameters
    ----------
    key : str
        Allow key of the form ``path`` or ``path::name``.
    location : Location
        Location reported by the detector.

    Returns
    -------
    bool
        ``True`` when the path glob matches and, for ``::name`` keys, the
        location carries exactly that unit name.

    Examples
    --------
    >>> location = Location(file="cuprum/echo.py", start=1, end=2, name="run")
    >>> key_matches("cuprum/*.py", location)
    True
    >>> key_matches("cuprum/*.py::other", location)
    False
    """
    path_glob, _, name = key.partition("::")
    if name and location.name != name:
        return False
    return PurePosixPath(location.file).full_match(path_glob)


def _validate_key_shape(
    path_glob: str, separator: str, name: str, *, context: str
) -> None:
    """Reject a key with an empty path or a ``::`` suffix naming no unit."""
    msg = f"{context} must be a 'path' or 'path::name' key"
    if not path_glob:
        raise GateConfigError(msg)
    if separator and not name:
        raise GateConfigError(msg)


def _validate_repository_relative_path(path_glob: str, *, context: str) -> None:
    """Reject a path glob that is absolute or escapes the repository root."""
    if path_glob.startswith("/") or ".." in PurePosixPath(path_glob).parts:
        msg = f"{context} must be a repository-relative path key"
        raise GateConfigError(msg)


def validate_key(key: str, *, context: str) -> str:
    """Validate one allow key and return it unchanged.

    Parameters
    ----------
    key : str
        Candidate ``path`` or ``path::name`` allow key.
    context : str
        Diagnostic prefix naming the source of the key.

    Returns
    -------
    str
        The validated key.

    Propagated errors
    -----------------
    GateConfigError
        If the key is not a well-formed ``path[::name]`` string.
    """
    path_glob, separator, name = key.partition("::")
    _validate_key_shape(path_glob, separator, name, context=context)
    _validate_repository_relative_path(path_glob, context=context)
    return key


def load_allowlist(pyproject_path: Path) -> tuple[AllowEntry, ...]:
    """Load the reasoned allow entries from ``[tool.duplication_gate]``.

    Parameters
    ----------
    pyproject_path : pathlib.Path
        Path to the repository ``pyproject.toml``.

    Returns
    -------
    tuple[AllowEntry, ...]
        Parsed allow entries in file order.

    Raises
    ------
    GateConfigError
        If an entry is missing a reason or names neither a unit nor members.
    """
    with pyproject_path.open("rb") as handle:
        data = tomllib.load(handle)
    root = _config_mapping(data, context="pyproject")
    tool = _config_mapping(root.get("tool", {}), context="pyproject.tool")
    table = _config_mapping(
        tool.get("duplication_gate", {}),
        context="pyproject.tool.duplication_gate",
    )
    raw_entries = table.get("allow", ())
    if not isinstance(raw_entries, cabc.Sequence) or isinstance(
        raw_entries, (str, bytes)
    ):
        msg = "duplication_gate.allow must be an array"
        raise GateConfigError(msg)
    return tuple(
        _allow_entry(raw, index=index) for index, raw in enumerate(raw_entries)
    )


def _config_mapping(value: object, *, context: str) -> cabc.Mapping[str, object]:
    """Validate one TOML table before configuration logic consumes it."""
    if not isinstance(value, cabc.Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        msg = f"{context} must be a table with string keys"
        raise GateConfigError(msg)
    return typ.cast("cabc.Mapping[str, object]", value)


def _allow_entry(raw: object, *, index: int) -> AllowEntry:
    """Validate and normalize one reasoned TOML allow entry."""
    table = _config_mapping(raw, context=f"duplication_gate.allow[{index}]")
    reason = table.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        msg = f"duplication_gate.allow[{index}] requires a non-empty reason"
        raise GateConfigError(msg)
    return AllowEntry(keys=_entry_keys(table, index), reason=reason)


def _unit_entry_keys(unit: object, *, context: str) -> tuple[str, ...]:
    """Validate a single-location ``unit`` field as a one-key tuple."""
    if not isinstance(unit, str):
        msg = f"{context} unit must be a 'path[::name]' string"
        raise GateConfigError(msg)
    return (validate_key(unit, context=f"{context} unit"),)


def _members_entry_keys(members: object, *, context: str) -> tuple[str, ...]:
    """Validate a multi-location ``members`` field, preserving its order."""
    if not _is_key_list(members):
        msg = f"{context} members must be two or more 'path[::name]' strings"
        raise GateConfigError(msg)
    return tuple(
        validate_key(member, context=f"{context} members") for member in members
    )


def _entry_keys(raw: cabc.Mapping[str, object], index: int) -> tuple[str, ...]:
    """Extract and validate the location keys named by one allow entry."""
    unit = raw.get("unit")
    members = raw.get("members")
    context = f"duplication_gate.allow[{index}]"
    if (unit is None) == (members is None):
        msg = f"{context} must set exactly one of 'unit' or 'members'"
        raise GateConfigError(msg)
    if unit is not None:
        return _unit_entry_keys(unit, context=context)
    return _members_entry_keys(members, context=context)


def _is_key_list(value: object) -> typ.TypeIs[cabc.Sequence[str]]:
    """Report whether ``value`` is an array of at least two key strings."""
    if not isinstance(value, cabc.Sequence) or isinstance(value, (str, bytes)):
        return False
    return len(value) >= _MINIMUM_MEMBER_COUNT and all(
        isinstance(member, str) for member in value
    )


def append_allow_entry(
    pyproject_path: Path,
    *,
    keys: cabc.Sequence[str],
    reason: str,
) -> None:
    """Append one allow entry to ``[tool.duplication_gate]`` in place.

    Parameters
    ----------
    pyproject_path : pathlib.Path
        Path to the ``pyproject.toml`` to update.
    keys : collections.abc.Sequence[str]
        One location key for a unit entry, or several for a members entry.
    reason : str
        Reviewable justification recorded with the entry.

    Raises
    ------
    GateConfigError
        If ``pyproject_path`` is unreadable or is not valid TOML.
    """
    target = tuple(keys)
    with _locked_file(pyproject_path):
        try:
            document = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
        except tomlkit.exceptions.ParseError as error:
            # tomlkit raises a ValueError subclass, which the gate's own
            # vocabulary does not catch, so an unparsable manifest would
            # otherwise escape as a traceback rather than a reported
            # configuration error.
            msg = f"cannot parse {pyproject_path}: {error}"
            raise GateConfigError(msg) from error
        tool = document.setdefault("tool", tomlkit.table(is_super_table=True))
        gate = tool.setdefault("duplication_gate", tomlkit.table())
        entries = gate.setdefault("allow", tomlkit.aot())
        for index, raw_entry in enumerate(entries):
            existing = _allow_entry(raw_entry, index=index)
            if _same_allow_target(existing.keys, target):
                raw_entry["reason"] = reason
                _write_document(pyproject_path, document)
                return
        entries.append(_new_entry(target, reason))
        _write_document(pyproject_path, document)


def _new_entry(keys: tuple[str, ...], reason: str) -> tomlkit.items.Table:
    """Build the TOML table recording one reasoned allow entry."""
    entry = tomlkit.table()
    if len(keys) == 1:
        entry["unit"] = keys[0]
    else:
        entry["members"] = list(keys)
    entry["reason"] = reason
    return entry


def _write_document(pyproject_path: Path, document: tomlkit.TOMLDocument) -> None:
    """Replace ``pyproject.toml`` atomically with the edited document."""
    atomic_write(
        pyproject_path,
        tomlkit.dumps(document).encode("utf-8"),
        options=AtomicWriteOptions(
            create_parents=False,
            preserve_mode=True,
            sync_file=True,
        ),
    )


def _same_allow_target(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Report whether two entries name the same unordered set of keys."""
    return left == right or (len(left) == len(right) and set(left) == set(right))


@contextmanager
def _locked_file(path: Path) -> cabc.Iterator[None]:
    """Hold an advisory cross-process lock while replacing ``path``."""
    lock_path = path.with_name(f".{path.name}.duplication-gate.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
