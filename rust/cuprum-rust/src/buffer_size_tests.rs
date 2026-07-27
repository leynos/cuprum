//! Boundary tests for `checked_buffer_size`.
//!
//! These pin the platform-boundary behaviour the fixed tests miss: every
//! non-positive value is rejected, every accepted value round-trips into a
//! `usize` at or below the cap, and out-of-range and over-cap values are
//! rejected with a stable message.

use proptest::prelude::*;

use crate::{MAX_BUFFER_SIZE, checked_buffer_size};

/// `MAX_BUFFER_SIZE` (1 GiB) expressed as `i64` for boundary construction.
/// Kept in sync with the library constant by [`cap_constant_matches_lib`].
const CAP: i64 = 1 << 30;

/// Reference implementation of the `checked_buffer_size` contract.
///
/// Encoded independently of the library function so [`matches_bounds`] can
/// compare the two directly across the full `i64` domain; the stable messages
/// mirror `checked_buffer_size` exactly.
fn expected_buffer_size_result(value: i64) -> Result<usize, &'static str> {
    if value <= 0 {
        return Err("buffer_size must be greater than zero");
    }
    let size = usize::try_from(value).map_err(|_| "buffer_size is too large")?;
    if size > MAX_BUFFER_SIZE {
        return Err("buffer_size exceeds the maximum permitted size");
    }
    Ok(size)
}

#[test]
fn cap_constant_matches_lib() {
    assert_eq!(usize::try_from(CAP), Ok(MAX_BUFFER_SIZE));
}

#[test]
fn rejects_zero_and_negative() {
    for value in [0_i64, -1, -65536, i64::MIN] {
        assert!(
            checked_buffer_size(value).is_err(),
            "{value} must be rejected",
        );
    }
}

#[test]
fn accepts_boundary_values() {
    assert_eq!(checked_buffer_size(1), Ok(1));
    assert_eq!(checked_buffer_size(CAP), Ok(MAX_BUFFER_SIZE));
}

#[test]
fn rejects_values_above_the_cap() {
    assert!(checked_buffer_size(CAP + 1).is_err());
    assert!(checked_buffer_size(i64::MAX).is_err());
}

proptest! {
    /// `checked_buffer_size` matches the reference contract across the full
    /// `i64` domain, returning the same value or the same stable message.
    #[test]
    fn matches_bounds(value in any::<i64>()) {
        prop_assert_eq!(checked_buffer_size(value), expected_buffer_size_result(value));
    }
}
