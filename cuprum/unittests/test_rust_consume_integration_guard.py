"""Guards on the deferred integration status of ``rust_consume_stream``.

ADR-002 ships ``rust_consume_stream`` in the wheel but deliberately does not
route production stream consumption through it yet. Two things have to stay
true while that holds: the symbol advertises its status in its own docstring,
and no production module calls it. These tests fail if either drifts, so the
Phase 2 decision is revisited rather than silently overtaken.

They are pure source inspection and never touch the compiled extension, which
is why they sit apart from the behavioural coverage in ``test_rust_streams.py``
and outwith the extension-gated target list.

Example
-------
pytest cuprum/unittests/test_rust_consume_integration_guard.py
"""

from __future__ import annotations

import ast
import pathlib
import typing as typ

import pytest


class _ModuleReferenceScanError(Exception):
    """Raised when a module cannot be inspected for production references."""

    def __init__(self, path: pathlib.Path, symbol: str) -> None:
        """Describe the module reference scan failure."""
        super().__init__(f"cannot inspect {path} for {symbol!r} production references")


def _module_references_symbol(path: pathlib.Path, symbol: str) -> bool:
    """Return whether *path* contains executable references to *symbol*."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise _ModuleReferenceScanError(path, symbol) from exc
    return any(_node_references_symbol(node, symbol) for node in ast.walk(tree))


def _node_references_symbol(node: ast.AST, symbol: str) -> bool:
    """Return whether an AST node references *symbol* as a Python symbol."""
    if isinstance(node, ast.Name):
        return node.id == symbol
    if isinstance(node, ast.Attribute):
        return node.attr == symbol
    if isinstance(node, ast.alias):
        return symbol in {node.name, node.asname}
    return False


@pytest.mark.parametrize(
    ("source", "expected_reference"),
    [
        ('"""rust_consume_stream is mentioned here."""\n', "ignored"),
        ("# rust_consume_stream is mentioned here.\n", "ignored"),
        ("rust_consume_stream(reader_fd)\n", "referenced"),
        ("streams.rust_consume_stream(reader_fd)\n", "referenced"),
        ("from cuprum._streams_rs import rust_consume_stream\n", "referenced"),
        ("if rust_consume_stream(\n", "invalid"),
    ],
)
def test_module_references_symbol_ignores_non_code_text(
    tmp_path: pathlib.Path,
    source: str,
    expected_reference: typ.Literal["ignored", "referenced", "invalid"],
) -> None:
    """The production-reference guard detects symbols, not raw text."""
    module_path = tmp_path / "candidate.py"
    module_path.write_text(source, encoding="utf-8")

    if expected_reference == "invalid":
        with pytest.raises(_ModuleReferenceScanError, match="cannot inspect"):
            _module_references_symbol(module_path, "rust_consume_stream")
        return

    has_reference = expected_reference == "referenced"
    assert (
        _module_references_symbol(module_path, "rust_consume_stream") is has_reference
    ), f"expected reference detection to report {expected_reference}"


def test_rust_consume_stream_docstring_not_integrated() -> None:
    """``rust_consume_stream`` documents its deferred integration status."""
    from cuprum import _streams_rs

    docstring = _streams_rs.rust_consume_stream.__doc__ or ""
    assert "not yet integrated" in docstring, (
        "rust_consume_stream must declare its not-integrated status"
    )


def test_rust_consume_stream_not_referenced_in_production() -> None:
    """Production code does not route consumes through ``rust_consume_stream``."""
    from cuprum import _streams_rs

    module_file = _streams_rs.__file__
    assert module_file is not None, "_streams_rs must be a file-backed module"
    package_root = pathlib.Path(module_file).parent
    scan_errors: list[str] = []
    referencing: list[pathlib.Path] = []
    for path in package_root.rglob("*.py"):
        if "unittests" in path.parts or path.name == "_streams_rs.py":
            continue
        try:
            if _module_references_symbol(path, "rust_consume_stream"):
                referencing.append(path.relative_to(package_root))
        except _ModuleReferenceScanError as exc:
            cause = exc.__cause__
            cause_context = (
                f"; caused by {type(cause).__name__}: {cause}"
                if cause is not None
                else ""
            )
            scan_errors.append(f"{exc}{cause_context}")

    assert scan_errors == [], (
        "could not inspect one or more production modules; "
        f"fix the underlying read/parse errors: {scan_errors}"
    )
    assert sorted(referencing) == [], (
        "production code now references rust_consume_stream; revisit the "
        f"ADR-002 Phase 2 decision and this guard: {sorted(referencing)}"
    )
