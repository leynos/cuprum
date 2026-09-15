//! Checked length kernels shared by production I/O and formal verification.
#![forbid(unsafe_code)]

/// Reject a native byte count outside the supplied initialized buffer.
#[must_use]
pub const fn checked_count(count: usize, capacity: usize) -> Option<usize> {
    if count <= capacity { Some(count) } else { None }
}

/// Advance byte accounting without overflow or consuming beyond a chunk.
///
/// Failure leaves both input values available unchanged to the caller.
#[must_use]
pub const fn checked_progress(total: u64, remaining: u64, written: u64) -> Option<(u64, u64)> {
    if written > remaining || written > u64::MAX - total {
        None
    } else {
        Some((total + written, remaining - written))
    }
}

#[cfg(test)]
mod tests {
    //! Boundary examples complement the symbolic domain checks.
    use super::{checked_count, checked_progress};

    #[test]
    fn rejects_invalid_bounds_and_overflow() {
        assert_eq!(checked_count(5, 4), None);
        assert_eq!(checked_count(4, 4), Some(4));
        assert_eq!(checked_progress(u64::MAX, 1, 1), None);
        assert_eq!(checked_progress(0, 4, 5), None);
        assert_eq!(checked_progress(u64::MAX - 1, 2, 1), Some((u64::MAX, 1)));
        assert_eq!(checked_progress(5, 4, 0), Some((5, 4)));
    }
}
