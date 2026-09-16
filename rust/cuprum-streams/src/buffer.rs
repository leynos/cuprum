//! Fallible scratch-buffer allocation for the stream loops.
//!
//! The pump, consume, and splice paths each need a zeroed buffer sized from
//! the caller's `buffer_size`. `vec![0_u8; len]` allocates infallibly: on
//! failure it runs the allocator's error handler, which aborts the process
//! and cannot be caught, so a bad `buffer_size` would take an embedding
//! interpreter down with it. Reserving fallibly keeps that failure on
//! [`PumpError`], where the Python boundary can raise it.
use crate::PumpError;

/// Allocate a zeroed scratch buffer of `len` bytes.
///
/// # Errors
/// Returns [`PumpError::BufferAllocationFailed`] when the allocation fails.
pub(crate) fn allocate_buffer(len: usize) -> Result<Vec<u8>, PumpError> {
    let mut buffer = Vec::new();
    buffer
        .try_reserve_exact(len)
        .map_err(|_| PumpError::BufferAllocationFailed)?;
    // The reservation above already guarantees the capacity, so growing to
    // `len` here cannot reallocate and cannot reach the aborting path.
    buffer.resize(len, 0_u8);
    Ok(buffer)
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
