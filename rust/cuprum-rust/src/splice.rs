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

use std::fs::File;
use std::io;
use std::os::unix::io::AsRawFd;

use crate::io_utils::is_nonfatal_write_error;

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
    reader: &File,
    writer: &File,
    chunk_size: usize,
) -> Option<Result<u64, io::Error>> {
    let reader_fd = reader.as_raw_fd();
    let writer_fd = writer.as_raw_fd();

    // Attempt first splice to detect support.
    match splice_once(reader_fd, writer_fd, chunk_size) {
        Ok(0) => Some(Ok(0)), // EOF on first call
        Ok(n) => Some(splice_loop(reader_fd, writer_fd, chunk_size, n)),
        Err(e) if is_splice_unsupported(&e) => None, // Fall back to read/write
        Err(e) if is_nonfatal_write_error(&e) => {
            // Broken pipe on first call: drain reader to avoid upstream deadlock.
            drain_reader(reader_fd, chunk_size);
            Some(Ok(0))
        }
        Err(e) => Some(Err(e)), // Fatal error
    }
}

/// Perform a single splice call.
fn splice_once(fd_in: libc::c_int, fd_out: libc::c_int, len: usize) -> Result<usize, io::Error> {
    let flags = SPLICE_F_MOVE | SPLICE_F_MORE;

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

    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        // Non-negative ssize_t fits in usize on Linux.
        Ok(result as usize)
    }
}

/// Continue splicing until EOF or error.
fn splice_loop(
    fd_in: libc::c_int,
    fd_out: libc::c_int,
    chunk_size: usize,
    initial_bytes: usize,
) -> Result<u64, io::Error> {
    let mut total = initial_bytes as u64;

    loop {
        match splice_once(fd_in, fd_out, chunk_size) {
            Ok(0) => break, // EOF
            Ok(n) => {
                total = total.saturating_add(n as u64);
            }
            Err(e) if is_nonfatal_write_error(&e) => {
                // Broken pipe: drain reader and return bytes written so far.
                drain_reader(fd_in, chunk_size);
                break;
            }
            Err(e) => return Err(e),
        }
    }

    Ok(total)
}

/// Drain remaining data from reader after broken pipe.
///
/// This matches the behaviour of the read/write fallback: when the writer
/// breaks, we continue reading until EOF to ensure the upstream process
/// does not block on a full pipe buffer.
fn drain_reader(fd_in: libc::c_int, chunk_size: usize) {
    let mut buf = vec![0u8; chunk_size];
    loop {
        // SAFETY: read is a well-defined syscall.
        let n = unsafe { libc::read(fd_in, buf.as_mut_ptr().cast(), buf.len()) };
        if n <= 0 {
            break;
        }
    }
}

/// Check if error indicates splice is not supported for these FDs.
///
/// These errors indicate the file descriptors do not support splice:
/// - `EINVAL`: Invalid argument (FD type not supported)
/// - `EBADF`: Bad file descriptor
/// - `ESPIPE`: Illegal seek (sometimes returned for unseekable FDs)
fn is_splice_unsupported(err: &io::Error) -> bool {
    matches!(
        err.raw_os_error(),
        Some(libc::EINVAL | libc::EBADF | libc::ESPIPE)
    )
}
