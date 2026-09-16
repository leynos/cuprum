//! Fallible scratch-buffer allocation for the stream loops.
//!
//! The pump, consume, and splice paths each need a zeroed buffer sized from
//! the caller's `buffer_size`. `vec![0_u8; len]` allocates infallibly: on
//! failure it runs the allocator's error handler, which aborts the process
//! and cannot be caught, so a bad `buffer_size` would take an embedding
//! interpreter down with it. Reserving fallibly keeps that failure on
//! [`PumpError`], where the Python boundary can raise it.
use crate::PumpError;

/// Host platform name, matching the `platform` field of the I/O seam events.
#[cfg(unix)]
const PLATFORM: &str = "unix";
/// Host platform name, matching the `platform` field of the I/O seam events.
#[cfg(windows)]
const PLATFORM: &str = "windows";

/// Allocate a zeroed scratch buffer of `len` bytes.
///
/// # Errors
/// Returns [`PumpError::BufferAllocationFailed`] when the allocation fails.
/// The failure is also reported as an `error` event carrying the stable
/// `buffer_allocation_failed` category and a power-of-two bucket of the
/// requested size, so a refused allocation is diagnosable without an unwind or
/// a core dump.
pub(crate) fn allocate_buffer(len: usize) -> Result<Vec<u8>, PumpError> {
    let mut buffer = Vec::new();
    if buffer.try_reserve_exact(len).is_err() {
        // Only the requested size is reported, and only as a power-of-two
        // bucket: no allocator internals, no identifiers, and no payload.
        tracing::error!(
            error_category = ALLOCATION_FAILED_CATEGORY,
            buffer_size = BufferBucket::of(len).bytes(),
            platform = PLATFORM,
            "scratch buffer allocation refused",
        );
        return Err(PumpError::BufferAllocationFailed);
    }
    // The reservation above already guarantees the capacity, so growing to
    // `len` here cannot reallocate and cannot reach the aborting path.
    buffer.resize(len, 0_u8);
    Ok(buffer)
}

/// Stable category reported by the allocation-failure error event.
pub(crate) const ALLOCATION_FAILED_CATEGORY: &str = "buffer_allocation_failed";

/// A requested buffer size rounded up to a power of two, for telemetry.
///
/// Rounding bounds the number of distinct values the event can carry and keeps
/// the logged number from leaking an exact caller-chosen size. The bucket is
/// always at least the request, so it still shows how large an allocation was
/// refused; a request too large to round is reported as itself, because no
/// larger power of two is representable.
#[derive(Clone, Copy, Debug)]
struct BufferBucket(usize);

impl BufferBucket {
    /// Round `requested` up to the next power of two, saturating at the top.
    const fn of(requested: usize) -> Self {
        match requested.checked_next_power_of_two() {
            Some(rounded) => Self(rounded),
            None => Self(requested),
        }
    }

    /// The rounded size in bytes.
    const fn bytes(self) -> usize { self.0 }
}

#[cfg(test)]
mod tests {
    //! Allocation coverage: usable buffers and, crucially, a failure that
    //! returns instead of aborting the test process.
    use super::allocate_buffer;
    use crate::PumpError;

    #[test]
    fn a_requested_buffer_is_zeroed_and_exactly_sized() {
        match allocate_buffer(8) {
            Ok(buffer) => assert_eq!(buffer, [0_u8; 8]),
            Err(error) => panic!("a small allocation must succeed, got {error:?}"),
        }
    }

    #[test]
    fn an_impossible_allocation_reports_rather_than_aborting() {
        // The production cap keeps real requests far below this, so the
        // capacity overflow stands in for any allocation the allocator
        // refuses: it exercises the same `try_reserve_exact` error path. A
        // regression to `vec![0_u8; len]` would abort here, failing the test
        // by killing the harness rather than by a failed assertion.
        let result = allocate_buffer(usize::MAX);
        assert!(
            matches!(result, Err(PumpError::BufferAllocationFailed)),
            "an unallocatable buffer must report, not abort",
        );
    }
}
