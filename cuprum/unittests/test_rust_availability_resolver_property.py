"""Properties for the public Rust-availability resolver boundary.

``is_rust_available()`` preserves both Boolean resolver answers exactly and
rejects every generated non-Boolean answer with ``TypeError``. The resolver is
patched at the public boundary's imported dependency, so the properties test
the runtime contract without changing global backend configuration.
"""

from __future__ import annotations

from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum import rust as rust_api

_NON_BOOLEAN_RESOLVER_VALUES = st.one_of(
    st.none(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=64),
    st.lists(st.integers(), max_size=4),
    st.dictionaries(st.text(max_size=8), st.integers(), max_size=4),
).filter(lambda value: not isinstance(value, bool))


@given(availability=_NON_BOOLEAN_RESOLVER_VALUES)
def test_rust_availability_rejects_every_non_boolean_resolver_value(
    availability: object,
) -> None:
    """The public resolver rejects every non-Boolean runtime result."""
    with (
        mock.patch.object(
            rust_api,
            "_check_rust_available",
            return_value=availability,
        ) as resolver,
        pytest.raises(TypeError, match="Rust availability resolver"),
    ):
        rust_api.is_rust_available()

    resolver.assert_called_once_with()


@given(availability=st.booleans())
def test_rust_availability_preserves_boolean_resolver_values(
    availability: bool,  # noqa: FBT001 - property input, not a flag.
) -> None:
    """The public resolver returns each valid Boolean answer unchanged."""
    with mock.patch.object(
        rust_api,
        "_check_rust_available",
        return_value=availability,
    ) as resolver:
        assert rust_api.is_rust_available() is availability

    resolver.assert_called_once_with()
