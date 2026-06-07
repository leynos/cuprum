//! I/O utilities for stream operations.
//!
//! This module provides helpers for reading and writing descriptor-backed
//! streams with proper error handling, including detection of non-fatal write
//! errors like broken pipes. All I/O operations emit [`tracing`] events so
//! callers can observe retry counts, bytes transferred, and error conditions
//! without installing a global subscriber.

use std::io;

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

/// Read bytes from the stream into the buffer.
pub(crate) fn read_stream(
    reader: &mut StreamHandle,
    buffer: &mut [u8],
) -> Result<usize, io::Error> {
    #[cfg(unix)]
    {
        read_stream_unix(reader, buffer)
    }

    #[cfg(windows)]
    {
        reader.read(buffer)
    }
}

/// Write all bytes from a chunk to the writer, returning bytes written.
pub(crate) fn handle_write(writer: &mut StreamHandle, chunk: &[u8]) -> Result<u64, io::Error> {
    #[cfg(unix)]
    write_all_unix(writer, chunk)?;

    #[cfg(windows)]
    writer.write_all(chunk)?;

    u64::try_from(chunk.len()).map_err(|_| io::Error::other("write length overflow"))
}

/// Check if an error is a non-fatal write condition (broken pipe).
///
/// These errors indicate the write end closed, which is expected when
/// downstream processes exit early. The caller should drain the reader
/// and return successfully rather than propagating the error.
pub(crate) fn is_nonfatal_write_error(err: &io::Error) -> bool {
    matches!(
        err.kind(),
        io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset
    )
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
) -> Result<bool, io::Error> {
    match handle_write(writer, chunk) {
        Ok(bytes) => {
            *total_written = total_written.saturating_add(bytes);
            tracing::debug!(bytes, total = *total_written, "write succeeded");
            Ok(true)
        }
        Err(err) => {
            if is_nonfatal_write_error(&err) {
                tracing::warn!(
                    error = ?err,
                    "non-fatal write error; signalling closed pipe to caller",
                );
                Ok(false)
            } else {
                tracing::error!(error = ?err, "fatal write error");
                Err(err)
            }
        }
    }
}

#[cfg(unix)]
fn read_stream_unix(reader: &StreamHandle, buffer: &mut [u8]) -> Result<usize, io::Error> {
    loop {
        // SAFETY: `buffer` is valid for writes of `buffer.len()` bytes, and
        // `reader` owns a valid descriptor for the duration of this call.
        let read_len = unsafe {
            libc::read(
                reader.as_raw_fd(),
                buffer.as_mut_ptr().cast::<libc::c_void>(),
                buffer.len(),
            )
        };

        if read_len >= 0 {
            let count =
                usize::try_from(read_len).map_err(|_| io::Error::other("read length overflow"))?;
            tracing::debug!(bytes = count, "read from descriptor");
            return Ok(count);
        }

        let err = io::Error::last_os_error();
        if err.kind() != io::ErrorKind::Interrupted {
            tracing::error!(error = ?err, "read syscall failed");
            return Err(err);
        }
        tracing::warn!("read syscall interrupted by signal; retrying");
    }
}

#[cfg(unix)]
fn write_all_unix(writer: &StreamHandle, mut chunk: &[u8]) -> Result<(), io::Error> {
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
            let written_len =
                usize::try_from(written).map_err(|_| io::Error::other("write length overflow"))?;
            tracing::debug!(
                written = written_len,
                remaining = chunk.len().saturating_sub(written_len),
                "partial write; advancing buffer",
            );
            chunk = chunk
                .get(written_len..)
                .ok_or_else(|| io::Error::other("write length exceeded buffer"))?;
            continue;
        }

        if written == 0 {
            tracing::error!("write syscall returned zero; aborting");
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "failed to write whole buffer",
            ));
        }

        let err = io::Error::last_os_error();
        if err.kind() != io::ErrorKind::Interrupted {
            tracing::error!(error = ?err, "write syscall failed");
            return Err(err);
        }
        tracing::warn!("write syscall interrupted by signal; retrying");
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io;

    #[test]
    fn broken_pipe_is_nonfatal() {
        let err = io::Error::from(io::ErrorKind::BrokenPipe);
        assert!(is_nonfatal_write_error(&err), "BrokenPipe must be non-fatal");
    }

    #[test]
    fn connection_reset_is_nonfatal() {
        let err = io::Error::from(io::ErrorKind::ConnectionReset);
        assert!(
            is_nonfatal_write_error(&err),
            "ConnectionReset must be non-fatal",
        );
    }

    #[test]
    fn permission_denied_is_fatal() {
        let err = io::Error::from(io::ErrorKind::PermissionDenied);
        assert!(
            !is_nonfatal_write_error(&err),
            "PermissionDenied must be fatal",
        );
    }

    #[test]
    fn timed_out_is_fatal() {
        let err = io::Error::from(io::ErrorKind::TimedOut);
        assert!(!is_nonfatal_write_error(&err), "TimedOut must be fatal");
    }

    #[cfg(unix)]
    mod unix {
        use std::io;
        use std::os::fd::{FromRawFd, OwnedFd};

        use crate::io_utils::{handle_write, handle_write_result, read_stream};

        /// Open an anonymous pipe and return `(read_end, write_end)`.
        #[must_use]
        fn make_pipe() -> Result<(OwnedFd, OwnedFd), io::Error> {
            let mut fds = [0i32; 2];
            // SAFETY: `fds` is a valid two-element array. `pipe(2)` fills it
            // with two freshly allocated, owned descriptors that we immediately
            // wrap in `OwnedFd` to enforce drop-on-close semantics.
            if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
                return Err(io::Error::last_os_error());
            }
            let [read_fd, write_fd] = fds;
            // SAFETY: `read_fd` and `write_fd` are valid, open, owned
            // descriptors returned by `pipe(2)` immediately above.
            Ok(unsafe {
                (
                    OwnedFd::from_raw_fd(read_fd),
                    OwnedFd::from_raw_fd(write_fd),
                )
            })
        }

        #[test]
        fn handle_write_sends_all_bytes() -> Result<(), io::Error> {
            let (mut reader, mut writer) = make_pipe()?;
            let payload = b"hello world";
            handle_write(&mut writer, payload)?;
            drop(writer);
            let mut buf = vec![0u8; payload.len()];
            let n = read_stream(&mut reader, &mut buf)?;
            assert_eq!(n, payload.len(), "byte count must match payload length");
            assert_eq!(buf, payload, "received bytes must match sent payload");
            Ok(())
        }

        #[test]
        fn read_stream_returns_zero_at_eof() -> Result<(), io::Error> {
            let (mut reader, writer) = make_pipe()?;
            drop(writer);
            let mut buf = [0u8; 8];
            let n = read_stream(&mut reader, &mut buf)?;
            assert_eq!(n, 0, "EOF must produce a zero-length read");
            Ok(())
        }

        #[test]
        fn read_stream_reads_partial_buffer() -> Result<(), io::Error> {
            let (mut reader, mut writer) = make_pipe()?;
            handle_write(&mut writer, b"hi")?;
            drop(writer);
            let mut buf = [0u8; 64];
            let n = read_stream(&mut reader, &mut buf)?;
            assert_eq!(n, 2, "read length must match bytes written");
            assert_eq!(buf.get(..2), Some(b"hi".as_slice()), "payload must round-trip");
            Ok(())
        }

        #[test]
        fn handle_write_result_accumulates_byte_count() -> Result<(), io::Error> {
            let (_reader, mut writer) = make_pipe()?;
            let mut total = 0u64;
            let open_after_first = handle_write_result(&mut writer, b"abc", &mut total)?;
            assert!(open_after_first, "pipe must remain open after first write");
            assert_eq!(total, 3, "total must reflect first write");
            let open_after_second = handle_write_result(&mut writer, b"de", &mut total)?;
            assert!(open_after_second, "pipe must remain open after second write");
            assert_eq!(total, 5, "total must reflect cumulative bytes written");
            Ok(())
        }
    }
}
