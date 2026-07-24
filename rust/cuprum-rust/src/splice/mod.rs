//! Linux-specific `splice()` optimization for zero-copy pipe transfers.
//!
//! This module provides zero-copy data transfer between pipe file descriptors
//! using the Linux `splice()` system call. When splice is not supported for the
//! given file descriptors, the caller should fall back to a read/write loop.
//!
//! # Behaviour
//!
//! The `try_splice_pump` function attempts to use splice for data transfer:
//!
//! - Returns `Some(Ok(bytes))` when splice succeeds and transfers all data
//! - Returns `Some(Err(e))` for fatal I/O errors that should propagate
//! - Returns `None` when splice is not supported, signaling fallback needed
//!
//! Splice requires at least one pipe endpoint. Regular files, most sockets,
//! and other file descriptor types return `EINVAL` and trigger the fallback.

use std::io;
use std::os::fd::{AsRawFd, OwnedFd};

use crate::errors::PumpError;
use crate::io_utils::read_raw_fd;

/// Flag for splice: move pages instead of copying (advisory).
const SPLICE_F_MOVE: libc::c_uint = 0x01;

/// Flag for splice: more data will be sent (for TCP corking).
const SPLICE_F_MORE: libc::c_uint = 0x04;

/// Attempt to pump data using `splice()`. Returns None if splice is not supported.
///
/// # Parameters
///
/// - `reader`: Source file (should be a pipe for splice to work)
/// - `writer`: Destination file (should be a pipe for splice to work)
/// - `chunk_size`: Maximum bytes to transfer per splice call
///
/// # Returns
///
/// - `Some(Ok(bytes))`: Successfully transferred `bytes` using splice
/// - `Some(Err(e))`: Fatal I/O error occurred
/// - `None`: splice not supported for these FDs; caller should use read/write
pub(crate) fn try_splice_pump(
    reader: &OwnedFd,
    writer: &OwnedFd,
    chunk_size: usize,
) -> Option<Result<u64, PumpError>> {
    let reader_fd = reader.as_raw_fd();
    let writer_fd = writer.as_raw_fd();

    // The first splice call detects support: `EINVAL` here means the FD
    // types cannot splice and the caller must fall back to read/write. Once
    // a splice has succeeded, `EINVAL` can no longer mean "unsupported", so
    // the loop below treats it as fatal like any other error.
    let first = splice_once(reader_fd, writer_fd, chunk_size);
    if matches!(&first, Err(e) if is_splice_unsupported(e)) {
        tracing::debug!("splice unsupported for these descriptors; using read/write fallback");
        return None;
    }
    Some(splice_loop(reader_fd, writer_fd, chunk_size, first))
}

/// Perform a single splice call, retrying on `EINTR`.
///
/// The `EINTR` retry lives at the syscall level, matching the Unix read and
/// write policies in [`crate::io_utils`]: a signal delivered mid-transfer
/// re-issues the splice rather than surfacing a spurious failure that would
/// abort an otherwise-healthy pump.
fn splice_once(fd_in: libc::c_int, fd_out: libc::c_int, len: usize) -> Result<usize, PumpError> {
    let flags = SPLICE_F_MOVE | SPLICE_F_MORE;
    splice_once_with(|| {
        // SAFETY: splice is a well-defined syscall; null offsets are valid for pipes.
        let result = unsafe {
            libc::splice(
                fd_in,
                std::ptr::null_mut(), // No offset for pipes
                fd_out,
                std::ptr::null_mut(), // No offset for pipes
                len,
                flags,
            )
        };

        if result >= 0 {
            Ok(result)
        } else {
            Err(io::Error::last_os_error())
        }
    })
}

/// Retry an interrupted splice syscall and map its result into [`PumpError`].
///
/// Factored out of [`splice_once`] so the `EINTR` retry policy can be
/// exercised deterministically without provoking a real signal, mirroring
/// [`crate::io_utils::read_raw_fd_with`]. Interrupted calls retry, a
/// non-negative result is the byte count, and every other error propagates.
fn splice_once_with(
    mut splice_call: impl FnMut() -> Result<libc::ssize_t, io::Error>,
) -> Result<usize, PumpError> {
    loop {
        match splice_call() {
            // Non-negative ssize_t fits in usize on Linux.
            Ok(transferred) => {
                return usize::try_from(transferred).map_err(|_| PumpError::LengthOverflow);
            }
            Err(err) if err.kind() == io::ErrorKind::Interrupted => {
                tracing::trace!("splice interrupted by a signal (EINTR); retrying");
            }
            Err(err) => return Err(PumpError::from(err)),
        }
    }
}

/// Splice until EOF or error, starting from a pre-computed first outcome.
///
/// This is the single canonical accumulation loop: every iteration —
/// including the first, whose outcome the caller supplies after the
/// support-detection check — is handled by the same `Ok(0)` / `Ok(n)` /
/// non-fatal / fatal arms. A non-fatal write error (broken pipe) drains the
/// reader so upstream does not block, then reports the bytes transferred so
/// far.
fn splice_loop(
    fd_in: libc::c_int,
    fd_out: libc::c_int,
    chunk_size: usize,
    first: Result<usize, PumpError>,
) -> Result<u64, PumpError> {
    accumulate_splices(
        first,
        || splice_once(fd_in, fd_out, chunk_size),
        || drain_reader(fd_in, chunk_size),
    )
}

/// Accumulate transferred bytes across splice outcomes until EOF or error.
///
/// This holds the loop's control flow with its side effects injected: `next`
/// yields each subsequent outcome (`splice_once` in production) and `drain`
/// empties the reader after a broken pipe (`drain_reader` in production).
/// Injecting them lets the accumulation and `Ok(0)` / `Ok(n)` / non-fatal /
/// fatal transitions be property-tested without real descriptors, while
/// production wiring keeps the single canonical loop.
fn accumulate_splices(
    first: Result<usize, PumpError>,
    mut next: impl FnMut() -> Result<usize, PumpError>,
    mut drain: impl FnMut() -> Result<(), PumpError>,
) -> Result<u64, PumpError> {
    let mut total = 0_u64;
    let mut outcome = first;

    loop {
        match outcome {
            Ok(0) => break, // EOF
            Ok(n) => {
                let chunk = u64::try_from(n).map_err(|_| PumpError::LengthOverflow)?;
                total = total.saturating_add(chunk);
            }
            Err(e) if e.is_nonfatal_write() => {
                // Broken pipe: drain reader and return bytes written so far.
                tracing::debug!(bytes_transferred = total, "broken pipe; draining reader");
                drain()?;
                break;
            }
            Err(e) => return Err(e),
        }
        outcome = next();
    }

    Ok(total)
}

/// Drain remaining data from reader after broken pipe.
///
/// This matches the behaviour of the read/write fallback: when the writer
/// breaks, we continue reading until EOF to ensure the upstream process
/// does not block on a full pipe buffer. The drain routes through the
/// canonical raw-fd read helper, so interrupted reads (`EINTR`) retry
/// instead of silently ending the drain, end of file terminates it, and
/// any other error propagates.
fn drain_reader(fd_in: libc::c_int, chunk_size: usize) -> Result<(), PumpError> {
    let mut buf = vec![0_u8; chunk_size];
    loop {
        let n = read_raw_fd(fd_in, &mut buf)?;
        if n == 0 {
            return Ok(());
        }
    }
}

/// Check if error indicates splice is not supported for these file descriptors.
///
/// Only `EINVAL` triggers a fallback to read/write: it indicates the file
/// descriptor type does not support splice (e.g., regular files, most sockets).
///
/// Other errors such as `EBADF` (bad file descriptor) or `ESPIPE` (illegal
/// seek) are configuration or programming errors that should propagate to the
/// caller rather than being masked by a fallback that will likely also fail.
fn is_splice_unsupported(err: &PumpError) -> bool {
    matches!(err, PumpError::Io(io_err) if io_err.raw_os_error() == Some(libc::EINVAL))
}

#[cfg(test)]
mod tests;
