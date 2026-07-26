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

They also fuzz ``_read_backend_env`` to prove invalid environment values are
rejected and that only the empty/``auto`` inputs resolve to ``AUTO``.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._backend import (
    StreamBackend,
    _read_backend_env,
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
def test_read_backend_env_rejects_invalid_values(raw: str) -> None:
    """Env parsing yields a valid member or raises; only empty/auto give AUTO."""
    normalised = raw.strip().lower()
    valid = {member.value for member in StreamBackend}
    prior = os.environ.get(_ENV_VAR)
    os.environ[_ENV_VAR] = raw
    try:
        if normalised == "" or normalised in valid:
            result = _read_backend_env()
            assert result in set(StreamBackend)
            if result is StreamBackend.AUTO:
                assert normalised in {"", "auto"}
        else:
            with pytest.raises(ValueError, match=_ENV_VAR):
                _read_backend_env()
    finally:
        if prior is None:
            os.environ.pop(_ENV_VAR, None)
        else:
            os.environ[_ENV_VAR] = prior
