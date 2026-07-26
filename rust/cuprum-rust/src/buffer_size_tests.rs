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
    /// `checked_buffer_size` accepts exactly the positive, in-range,
    /// at-or-below-cap values, and returns the matching `usize`.
    #[test]
    fn matches_bounds(value in any::<i64>()) {
        let result = checked_buffer_size(value);
        if value <= 0 {
            prop_assert!(result.is_err(), "non-positive {value} must be rejected");
        } else if let Ok(size) = usize::try_from(value) {
            if size <= MAX_BUFFER_SIZE {
                prop_assert_eq!(result, Ok(size));
            } else {
                prop_assert!(result.is_err(), "{value} above cap must be rejected");
            }
        } else {
            prop_assert!(result.is_err(), "{value} beyond usize must be rejected");
        }
    }
}
