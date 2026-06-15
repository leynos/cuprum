//! I/O utilities for stream operations.
//!
//! This module provides helpers for reading and writing descriptor-backed
//! streams with proper error handling, including detection of non-fatal write
//! errors like broken pipes. Failures are reported through the crate's
//! canonical [`PumpError`] taxonomy.

use std::io;

use crate::errors::PumpError;

#[cfg(unix)]
use std::os::fd::{AsRawFd, OwnedFd};

#[cfg(windows)]
use std::fs::File;

#[cfg(windows)]
use std::io::{Read, Write};

#[cfg(unix)]
pub(crate) type StreamHandle = OwnedFd;

#[cfg(windows)]
pub(crate) type StreamHandle = File;

/// Result of a single write attempt on the stream.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum WriteOutcome {
    /// The full chunk was written successfully; contains the byte count.
    Complete(u64),
    /// The write stopped due to a non-fatal broken-pipe/connection-reset error.
    /// The value is the number of bytes accepted before that error occurred,
    /// and it may be zero.
    NonFatalShortWrite(u64),
}

/// Read bytes from the stream into the buffer.
pub(crate) fn read_stream(
    reader: &mut StreamHandle,
    buffer: &mut [u8],
) -> Result<usize, PumpError> {
    #[cfg(unix)]
    {
        read_stream_unix(reader, buffer)
    }

    #[cfg(windows)]
    {
        reader.read(buffer).map_err(PumpError::from)
    }
}

/// Write all bytes from a chunk to the writer, returning the write outcome.
pub(crate) fn handle_write(
    writer: &mut StreamHandle,
    chunk: &[u8],
) -> Result<WriteOutcome, PumpError> {
    #[cfg(unix)]
    let outcome = write_all_unix(writer, chunk)?;

    #[cfg(windows)]
    let outcome = write_all_windows(writer, chunk)?;

    Ok(outcome)
}

/// Attempt to write a chunk and update the total written count.
///
/// Returns `Ok(true)` if the write succeeded and more writes are possible.
/// Returns `Ok(false)` if the pipe is broken (caller should drain reader).
/// Returns `Err` for fatal I/O errors.
pub(crate) fn handle_write_result(
    writer: &mut StreamHandle,
    chunk: &[u8],
    total_written: &mut u64,
) -> Result<bool, PumpError> {
    match handle_write(writer, chunk) {
        Ok(WriteOutcome::Complete(bytes)) => {
            *total_written = total_written.saturating_add(bytes);
            Ok(true)
        }
        Ok(WriteOutcome::NonFatalShortWrite(bytes)) => {
            *total_written = total_written.saturating_add(bytes);
            Ok(false)
        }
        Err(err) => {
            if err.is_nonfatal_write() {
                Ok(false)
            } else {
                Err(err)
            }
        }
    }
}

#[cfg(unix)]
fn read_stream_unix(reader: &StreamHandle, buffer: &mut [u8]) -> Result<usize, PumpError> {
    read_raw_fd(reader.as_raw_fd(), buffer)
}

/// Read from a raw descriptor, retrying on `EINTR`.
///
/// This is the canonical Unix read policy shared by the stream read path and
/// the splice drain: interrupted reads retry, end of file returns zero, and
/// every other error propagates.
#[cfg(unix)]
pub(crate) fn read_raw_fd(fd: libc::c_int, buffer: &mut [u8]) -> Result<usize, PumpError> {
    read_raw_fd_with(|| {
        // SAFETY: `buffer` is valid for writes of `buffer.len()` bytes, and
        // the caller guarantees `fd` stays valid for the duration of this
        // call.
        let read_len =
            unsafe { libc::read(fd, buffer.as_mut_ptr().cast::<libc::c_void>(), buffer.len()) };

        if read_len >= 0 {
            Ok(read_len)
        } else {
            Err(io::Error::last_os_error())
        }
    })
}

#[cfg(unix)]
fn read_raw_fd_with(
    mut read_once: impl FnMut() -> Result<libc::ssize_t, io::Error>,
) -> Result<usize, PumpError> {
    loop {
        match read_once() {
            Ok(read_len) => {
                return usize::try_from(read_len).map_err(|_| PumpError::LengthOverflow);
            }
            Err(err) if err.kind() == io::ErrorKind::Interrupted => {}
            Err(err) => return Err(PumpError::from(err)),
        }
    }
}

#[cfg(unix)]
fn write_all_unix(writer: &StreamHandle, mut chunk: &[u8]) -> Result<WriteOutcome, PumpError> {
    let mut total_written = 0_u64;

    while !chunk.is_empty() {
        // SAFETY: `chunk` is valid for reads of `chunk.len()` bytes, and
        // `writer` owns a valid descriptor for the duration of this call.
        let written = unsafe {
            libc::write(
                writer.as_raw_fd(),
                chunk.as_ptr().cast::<libc::c_void>(),
                chunk.len(),
            )
        };

        if written > 0 {
            let written_len = usize::try_from(written).map_err(|_| PumpError::LengthOverflow)?;
            record_write_progress(&mut chunk, written_len, &mut total_written)?;
            continue;
        }

        if written == 0 {
            return Err(PumpError::from(io::Error::new(
                io::ErrorKind::WriteZero,
                "failed to write whole buffer",
            )));
        }

        let err = io::Error::last_os_error();
        if err.kind() != io::ErrorKind::Interrupted {
            return map_short_write_error(err, total_written);
        }
    }

    Ok(WriteOutcome::Complete(total_written))
}

#[cfg(windows)]
fn write_all_windows(
    writer: &mut StreamHandle,
    mut chunk: &[u8],
) -> Result<WriteOutcome, PumpError> {
    let mut total_written = 0_u64;

    while !chunk.is_empty() {
        match writer.write(chunk) {
            Ok(0) => {
                return Err(PumpError::from(io::Error::new(
                    io::ErrorKind::WriteZero,
                    "failed to write whole buffer",
                )));
            }
            Ok(written_len) => {
                record_write_progress(&mut chunk, written_len, &mut total_written)?;
            }
            Err(err) if err.kind() == io::ErrorKind::Interrupted => {}
            Err(err) => {
                return map_short_write_error(err, total_written);
            }
        }
    }

    Ok(WriteOutcome::Complete(total_written))
}

fn record_write_progress(
    chunk: &mut &[u8],
    written_len: usize,
    total_written: &mut u64,
) -> Result<(), PumpError> {
    let written_len_u64 = u64::try_from(written_len).map_err(|_| PumpError::LengthOverflow)?;
    *total_written = total_written.saturating_add(written_len_u64);
    *chunk = chunk
        .get(written_len..)
        .ok_or(PumpError::BufferRangeExceeded)?;
    Ok(())
}

fn map_short_write_error(err: io::Error, total_written: u64) -> Result<WriteOutcome, PumpError> {
    let pump_error = PumpError::from(err);
    if pump_error.is_nonfatal_write() {
        return Ok(WriteOutcome::NonFatalShortWrite(total_written));
    }
    Err(pump_error)
}

#[cfg(test)]
mod tests {
    //! Direct tests for descriptor-backed I/O helper contracts.

    use std::fmt::Debug;
    use std::io::{self, Write};
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

    use super::{
        PumpError, WriteOutcome, handle_write, handle_write_result, map_short_write_error,
        read_raw_fd, read_raw_fd_with, read_stream,
    };

    fn pipe_pair() -> (OwnedFd, OwnedFd) {
        let mut fds = [0_i32; 2];
        // SAFETY: `fds` is a valid two-element array for `pipe(2)` to fill.
        let rc = unsafe { libc::pipe(fds.as_mut_ptr()) };
        assert_eq!(rc, 0, "pipe(2) failed: {}", io::Error::last_os_error());

        // SAFETY: on success `pipe(2)` returned two freshly opened FDs that
        // this process exclusively owns.
        unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) }
    }

    fn write_all_to(fd: &OwnedFd, payload: &[u8]) {
        // SAFETY: duplicating an owned descriptor for a scoped File wrapper.
        let duplicated_fd = unsafe { libc::dup(fd.as_raw_fd()) };
        assert_ne!(
            duplicated_fd,
            -1,
            "dup(2) failed: {}",
            io::Error::last_os_error(),
        );
        // SAFETY: `duplicated_fd` was checked for `dup(2)` failure above and
        // is now owned by this scoped `File`.
        let mut file = unsafe { std::fs::File::from_raw_fd(duplicated_fd) };
        unwrap_ok(file.write_all(payload));
    }

    fn unwrap_ok<T, E: Debug>(result: Result<T, E>) -> T {
        match result {
            Ok(value) => value,
            Err(err) => panic!("expected Ok(..), got Err({err:?})"),
        }
    }

    fn unwrap_err<T: Debug, E>(result: Result<T, E>) -> E {
        match result {
            Ok(value) => panic!("expected Err(..), got Ok({value:?})"),
            Err(err) => err,
        }
    }

    #[test]
    fn read_stream_reads_pipe_bytes() {
        let (mut read_end, write_end) = pipe_pair();
        write_all_to(&write_end, b"chunk");
        drop(write_end);
        let mut buffer = [0_u8; 8];

        let read_len = unwrap_ok(read_stream(&mut read_end, &mut buffer));

        assert_eq!(read_len, 5);
        assert_eq!(buffer.get(..read_len), Some(&b"chunk"[..]));
    }

    #[test]
    fn read_stream_reports_unreadable_descriptor() {
        let (_read_end, mut write_end) = pipe_pair();
        let mut buffer = [0_u8; 8];

        let err = unwrap_err(read_stream(&mut write_end, &mut buffer));

        assert!(matches!(err, PumpError::Io(_)));
    }

    #[test]
    fn read_raw_fd_reports_eof() {
        let (read_end, write_end) = pipe_pair();
        drop(write_end);
        let mut buffer = [0_u8; 8];

        let read_len = unwrap_ok(read_raw_fd(read_end.as_raw_fd(), &mut buffer));

        assert_eq!(read_len, 0);
    }

    #[test]
    fn read_raw_fd_retries_after_interruption() {
        let mut attempts = 0_u8;

        let read_len = unwrap_ok(read_raw_fd_with(|| {
            attempts = attempts.saturating_add(1);
            if attempts == 1 {
                return Err(io::Error::from(io::ErrorKind::Interrupted));
            }
            Ok(0)
        }));

        assert_eq!(read_len, 0);
        assert_eq!(attempts, 2);
    }

    #[test]
    fn handle_write_returns_complete_outcome() {
        let (read_end, mut write_end) = pipe_pair();

        let outcome = unwrap_ok(handle_write(&mut write_end, b"chunk"));

        assert_eq!(outcome, WriteOutcome::Complete(5));
        drop(read_end);
    }

    #[test]
    fn handle_write_reports_unwritable_descriptor() {
        let (mut read_end, _write_end) = pipe_pair();

        let err = unwrap_err(handle_write(&mut read_end, b"chunk"));

        assert!(matches!(err, PumpError::Io(_)));
    }

    #[test]
    fn handle_write_result_updates_total_on_success() {
        let (read_end, mut write_end) = pipe_pair();
        let mut total_written = 7_u64;

        let should_continue = unwrap_ok(handle_write_result(
            &mut write_end,
            b"chunk",
            &mut total_written,
        ));

        assert!(should_continue);
        assert_eq!(total_written, 12);
        drop(read_end);
    }

    #[test]
    fn handle_write_result_preserves_total_on_fatal_error() {
        let (mut read_end, _write_end) = pipe_pair();
        let mut total_written = 7_u64;

        let err = unwrap_err(handle_write_result(
            &mut read_end,
            b"chunk",
            &mut total_written,
        ));

        assert!(matches!(err, PumpError::Io(_)));
        assert_eq!(total_written, 7);
    }

    #[test]
    fn nonfatal_short_write_records_accepted_bytes() {
        let outcome = unwrap_ok(map_short_write_error(
            io::Error::from(io::ErrorKind::BrokenPipe),
            3,
        ));

        assert_eq!(outcome, WriteOutcome::NonFatalShortWrite(3));
    }

    #[test]
    fn fatal_short_write_errors_propagate() {
        let err = unwrap_err(map_short_write_error(
            io::Error::from(io::ErrorKind::PermissionDenied),
            3,
        ));

        assert!(matches!(err, PumpError::Io(_)));
    }
}
