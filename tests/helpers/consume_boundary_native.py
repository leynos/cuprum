"""Import and drive the compiled consume extension for the boundary oracle.

This module is the access layer only: it imports ``cuprum._streams_rs`` and
``cuprum._rust_backend_native``, describes the operations each must provide,
and offers the five independently callable consume variants. It does not judge
the text they return.

``tests/helpers/consume_boundary_oracle.py`` is its only caller. That split is
deliberate: the oracle owns which consumers are compared and what the expected
text is, while this module owns how a descriptor reaches the extension. Keeping
them together pushed the oracle past the repository's 400-line module cap, and
the two responsibilities separate cleanly.

Provenance is checked by the oracle rather than here, because "is this the
extension belonging to this project" is a question about the comparison being
trustworthy, not about how to call the extension.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import importlib
import os
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib


class _StreamsRust(typ.Protocol):
    """Describe the wrapper operations used by the oracle."""

    def _convert_fd_for_platform(self, reader_fd: int) -> int:
        """Convert a Python descriptor for the native extension."""
        ...

    def _duplicate_binary_reader(
        self, reader_fd: int
    ) -> contextlib.AbstractContextManager[int]:
        """Duplicate a Windows CRT reader in binary mode."""
        ...

    def rust_consume_stream(
        self,
        reader_fd: int,
        *,
        buffer_size: int = 65536,
    ) -> str:
        """Consume bytes through the Python extension wrapper."""
        ...


class _NativeExtension(typ.Protocol):
    """Describe the direct native consumer used by the oracle."""

    def rust_consume_stream(
        self,
        reader_fd: int,
        *,
        buffer_size: int = 65536,
    ) -> str:
        """Consume bytes through the native extension API."""
        ...


@dc.dataclass(frozen=True, slots=True)
class NativeModules:
    """Hold the imported wrapper, extension, and resolved extension path."""

    streams_rs: _StreamsRust
    native: _NativeExtension
    native_path: Path


def load_native_modules() -> NativeModules:
    """Import the wrapper and the compiled extension.

    Returns
    -------
    NativeModules
        The imported modules and the resolved path of the extension.
    """
    streams_rs = typ.cast(
        "_StreamsRust",
        importlib.import_module("cuprum._streams_rs"),
    )
    native_module = importlib.import_module("cuprum._rust_backend_native")
    native_path = Path(typ.cast("str", native_module.__file__)).resolve()
    native = typ.cast("_NativeExtension", native_module)
    return NativeModules(streams_rs, native, native_path)


def consume_direct(
    modules: NativeModules,
    reader_fd: int,
    *,
    buffer_size: int | None = None,
) -> str:
    """Call the extension directly with an independently owned reader copy."""
    if os.name == "nt":
        with modules.streams_rs._duplicate_binary_reader(reader_fd) as native_fd:
            return _call_native_consumer(modules, native_fd, buffer_size)

    native_fd = modules.streams_rs._convert_fd_for_platform(reader_fd)
    return _call_native_consumer(modules, native_fd, buffer_size)


def _call_native_consumer(
    modules: NativeModules,
    native_fd: int,
    buffer_size: int | None,
) -> str:
    """Call the direct extension method with omitted or explicit buffer size."""
    if buffer_size is None:
        return modules.native.rust_consume_stream(native_fd)
    return modules.native.rust_consume_stream(native_fd, buffer_size=buffer_size)


def native_consumers(
    modules: NativeModules,
    buffer_size: int,
) -> tuple[tuple[str, cabc.Callable[[int], object]], ...]:
    """Return independently callable wrapper and direct-native variants."""
    return (
        (
            "wrapper omitted buffer",
            functools.partial(modules.streams_rs.rust_consume_stream),
        ),
        (
            "wrapper explicit default",
            functools.partial(
                modules.streams_rs.rust_consume_stream, buffer_size=65536
            ),
        ),
        (
            "wrapper generated buffer",
            functools.partial(
                modules.streams_rs.rust_consume_stream, buffer_size=buffer_size
            ),
        ),
        ("direct native omitted buffer", functools.partial(consume_direct, modules)),
        (
            "direct native explicit default",
            functools.partial(consume_direct, modules, buffer_size=65536),
        ),
    )
