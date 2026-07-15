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
        return None;
    }
    Some(splice_loop(reader_fd, writer_fd, chunk_size, first))
}

/// Perform a single splice call.
fn splice_once(fd_in: libc::c_int, fd_out: libc::c_int, len: usize) -> Result<usize, PumpError> {
    let flags = SPLICE_F_MOVE | SPLICE_F_MORE;

    loop {
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
            // Non-negative ssize_t fits in usize on Linux.
            return usize::try_from(result).map_err(|_| PumpError::LengthOverflow);
        }

        let err = io::Error::last_os_error();
        if err.kind() != io::ErrorKind::Interrupted {
            return Err(PumpError::from(err));
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
    mut outcome: Result<usize, PumpError>,
) -> Result<u64, PumpError> {
    let mut total = 0_u64;

    loop {
        match outcome {
            Ok(0) => break, // EOF
            Ok(n) => {
                let chunk = u64::try_from(n).map_err(|_| PumpError::LengthOverflow)?;
                total = total.saturating_add(chunk);
            }
            Err(e) if e.is_nonfatal_write() => {
                // Broken pipe: drain reader and return bytes written so far.
                drain_reader(fd_in, chunk_size)?;
                break;
            }
            Err(e) => return Err(e),
        }
        outcome = splice_once(fd_in, fd_out, chunk_size);
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
mod tests {
    use crate::test_support::{make_pipe, read_all_from, unwrap_ok, write_all_to};

    #[test]

    fn splice_transfers_all_bytes_between_pipes() {
        let (source_read, source_write) = make_pipe();
        let (sink_read, sink_write) = make_pipe();
        let payload = b"unified splice loop payload";

        write_all_to(&source_write, payload);
        drop(source_write); // EOF for the splice loop.

        let outcome = try_splice_pump(&source_read, &sink_write, 4096);
        drop(sink_write); // EOF for the verification read.

        let expected_len = unwrap_ok(u64::try_from(payload.len()));
        match outcome {
            Some(Ok(transferred)) => {
                assert_eq!(transferred, expected_len);
            }
            other => panic!("expected Some(Ok(_)) from pipe splice, got {other:?}"),
        }
        assert_eq!(read_all_from(&sink_read), payload);
    }

    #[test]

    fn unsupported_descriptors_signal_fallback() {
        let dir = std::env::temp_dir();
        let reader_path = dir.join("cuprum-splice-test-reader");
        let writer_path = dir.join("cuprum-splice-test-writer");
        unwrap_ok(std::fs::write(&reader_path, b"file payload"));
        let reader = OwnedFd::from(unwrap_ok(File::open(&reader_path)));
        let writer = OwnedFd::from(unwrap_ok(File::create(&writer_path)));

        // Two regular files cannot splice; the first call must signal the
        // read/write fallback rather than erroring.
        let outcome = try_splice_pump(&reader, &writer, 4096);
        assert!(
            outcome.is_none(),
            "expected fallback signal, got {outcome:?}"
        );

        unwrap_ok(std::fs::remove_file(&reader_path));
        unwrap_ok(std::fs::remove_file(&writer_path));
    }

    #[test]

    fn broken_pipe_drains_reader_and_reports_transferred_bytes() {
        let (source_read, source_write) = make_pipe();
        let (sink_read, sink_write) = make_pipe();
        let payload = b"bytes that can no longer be delivered";

        write_all_to(&source_write, payload);
        drop(source_write);
        drop(sink_read); // Break the downstream pipe before pumping.

        let outcome = try_splice_pump(&source_read, &sink_write, 4096);
        match outcome {
            Some(Ok(0)) => {}
            other => panic!("expected Some(Ok(0)) after broken pipe, got {other:?}"),
        }

        // The drain must have consumed the source to EOF so upstream writers
        // cannot block on a full pipe buffer.
        let leftover = read_all_from(&source_read);
        assert!(
            leftover.is_empty(),
            "reader must be drained, got {leftover:?}"
        );
    }

    #[test]

    fn drain_reader_consumes_to_eof() {
        let (read_end, write_end) = make_pipe();
        write_all_to(&write_end, b"residual data");
        drop(write_end);

        unwrap_ok(drain_reader(read_end.as_raw_fd(), 8));

        let leftover = read_all_from(&read_end);
        assert!(leftover.is_empty(), "drain must consume the pipe to EOF");
    }
}
