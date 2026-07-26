"""Property-based tests for the pure stream-backend resolution core.

``cuprum._backend`` now splits a pure ``_resolve_backend(requested,
rust_available)`` decision from the cached, environment-reading, logging
wrapper. The pure core takes no environment and performs no I/O, so these
properties can pin its contract exhaustively:

- ``StreamBackend.AUTO`` never leaks out of the resolver.
- ``PYTHON`` is honoured unconditionally.
- Forced ``RUST`` succeeds iff the extension is available, otherwise raises
  ``ImportError``.
- ``AUTO`` selects ``RUST`` iff the extension is available.

They also fuzz ``_parse_backend_value`` (the pure parsing core behind
``_read_backend_env``) to prove invalid values are rejected and that only the
empty/``auto`` inputs resolve to ``AUTO`` — injecting the raw value directly
rather than mutating ``os.environ``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._backend import (
    StreamBackend,
    _parse_backend_value,
    _resolve_backend,
)

_ENV_VAR = "CUPRUM_STREAM_BACKEND"
_AVAILABILITY = st.sampled_from([True, False, None])
_REQUESTED = st.sampled_from(list(StreamBackend))


@given(requested=_REQUESTED, rust_available=_AVAILABILITY)
def test_resolver_never_returns_auto(
    requested: StreamBackend,
    rust_available: bool | None,  # noqa: FBT001 - property input, not a flag.
) -> None:
    """The resolver always collapses to a concrete backend (never ``AUTO``)."""
    try:
        resolved = _resolve_backend(requested, rust_available=rust_available)
    except ImportError:
        # Forced Rust without availability is the only permitted failure.
        assert requested is StreamBackend.RUST
        assert not rust_available
        return
    assert resolved in {StreamBackend.RUST, StreamBackend.PYTHON}


@given(rust_available=_AVAILABILITY)
def test_python_is_honoured_unconditionally(
    rust_available: bool | None,  # noqa: FBT001 - property input, not a flag.
) -> None:
    """Requesting ``PYTHON`` always resolves to ``PYTHON``."""
    assert (
        _resolve_backend(StreamBackend.PYTHON, rust_available=rust_available)
        is StreamBackend.PYTHON
    )


@given(rust_available=_AVAILABILITY)
def test_forced_rust_respects_availability(
    rust_available: bool | None,  # noqa: FBT001 - property input, not a flag.
) -> None:
    """Forced ``RUST`` resolves iff available, else raises ``ImportError``."""
    if rust_available:
        assert (
            _resolve_backend(StreamBackend.RUST, rust_available=rust_available)
            is StreamBackend.RUST
        )
    else:
        with pytest.raises(ImportError, match="Rust extension is not available"):
            _resolve_backend(StreamBackend.RUST, rust_available=rust_available)


@given(rust_available=_AVAILABILITY)
def test_auto_follows_availability(
    rust_available: bool | None,  # noqa: FBT001 - property input, not a flag.
) -> None:
    """``AUTO`` picks ``RUST`` iff available, otherwise ``PYTHON``."""
    resolved = _resolve_backend(StreamBackend.AUTO, rust_available=rust_available)
    expected = StreamBackend.RUST if rust_available else StreamBackend.PYTHON
    assert resolved is expected


# Environment values can never contain a NUL byte (the OS rejects them), so
# fuzz over NUL-free text. Seed the known tokens (with case/whitespace noise)
# so the accept paths are exercised alongside the garbage.
_ENV_VALUE = st.one_of(
    st.text(
        alphabet=st.characters(codec="ascii", exclude_characters="\x00"),
        max_size=12,
    ),
    st.sampled_from(["", "auto", "RUST", " Python ", "\tauto\n", "turbo", "rustc"]),
)


@given(raw=_ENV_VALUE)
def test_parse_backend_value_maps_exactly_or_rejects(raw: str) -> None:
    """Parsing maps to the exact member (empty/auto -> AUTO), else raises.

    The raw value is injected directly rather than written to ``os.environ``,
    so the property never mutates global process state (avoiding the shared
    environment-guard requirement in AGENTS.md and any cross-thread races).
    """
    normalised = raw.strip().lower()
    valid = {member.value for member in StreamBackend}
    if normalised == "":
        # Empty/whitespace resolves to AUTO.
        assert _parse_backend_value(raw) is StreamBackend.AUTO
    elif normalised in valid:
        # A recognized value maps to exactly that member, never a different one.
        assert _parse_backend_value(raw).value == normalised
    else:
        with pytest.raises(ValueError, match=_ENV_VAR):
            _parse_backend_value(raw)
