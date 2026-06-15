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
pub(crate) enum WriteOutcome {
    /// The full chunk was written successfully; contains the byte count.
    Complete(u64),
    /// A non-fatal pipe closure occurred after this many bytes were written.
    ///
    /// This covers broken pipes and connection resets after partial progress.
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
    loop {
        // SAFETY: `buffer` is valid for writes of `buffer.len()` bytes, and
        // the caller guarantees `fd` stays valid for the duration of this
        // call.
        let read_len =
            unsafe { libc::read(fd, buffer.as_mut_ptr().cast::<libc::c_void>(), buffer.len()) };

        if read_len >= 0 {
            return usize::try_from(read_len).map_err(|_| PumpError::LengthOverflow);
        }

        let err = io::Error::last_os_error();
        if err.kind() != io::ErrorKind::Interrupted {
            return Err(PumpError::from(err));
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
