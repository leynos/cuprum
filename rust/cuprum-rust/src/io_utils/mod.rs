//! I/O utilities for stream operations.
//!
//! This module provides helpers for reading and writing descriptor-backed
//! streams with proper error handling, including detection of non-fatal write
//! errors like broken pipes. Failures are reported through the crate's
//! canonical [`PumpError`] taxonomy.

use std::cell::Cell;
use std::io;

use crate::errors::PumpError;

thread_local! {
    /// EINTR retries observed on the read path of the current operation.
    static READ_RETRIES: Cell<u64> = const { Cell::new(0) };
    /// EINTR retries observed on the write path of the current operation.
    static WRITE_RETRIES: Cell<u64> = const { Cell::new(0) };
}

/// Reset the per-operation retry counters.
///
/// Called when a pump/consume operation enters its span so retry counts are
/// scoped to that operation and never leak across operations that reuse the
/// same OS thread. The pump/consume loop runs on a single thread (the GIL is
/// released for the duration), so thread-local accumulation matches the
/// operation exactly without threading a counter through every seam.
pub(crate) fn reset_retry_counters() {
    READ_RETRIES.with(|counter| counter.set(0));
    WRITE_RETRIES.with(|counter| counter.set(0));
}

/// EINTR retries accumulated on the read path since the last reset.
pub(crate) fn read_retry_count() -> u64 {
    READ_RETRIES.with(Cell::get)
}

/// EINTR retries accumulated on the write path since the last reset.
pub(crate) fn write_retry_count() -> u64 {
    WRITE_RETRIES.with(Cell::get)
}

fn record_read_retry() {
    READ_RETRIES.with(|counter| counter.set(counter.get().saturating_add(1)));
}

fn record_write_retry() {
    WRITE_RETRIES.with(|counter| counter.set(counter.get().saturating_add(1)));
}

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

/// Build the tracing span that wraps a read/write pump operation.
///
/// The span is created at ERROR level, not INFO, so the read/write seams'
/// EINTR `warn!` and fatal-I/O `error!` events inherit the `operation`,
/// `buffer_size`, and `total_bytes` context even when the subscriber is
/// filtered to `warn`/`error` — the conventional production configuration.
/// Under such a filter an INFO-level span is disabled, dropping the operation
/// context from exactly the events operators depend on. ERROR is the least
/// verbose level, so the span stays enabled under both `warn`- and
/// `error`-only filters. The span never emits a log line on its own (no span
/// lifecycle events are subscribed), so raising its level adds no output; the
/// caller records `total_bytes` on completion.
pub(crate) fn operation_span(operation: &'static str, buffer_size: usize) -> tracing::Span {
    tracing::error_span!(
        "stream_pump",
        operation,
        buffer_size,
        total_bytes = tracing::field::Empty,
        read_retries = tracing::field::Empty,
        write_retries = tracing::field::Empty,
    )
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
                let len = usize::try_from(read_len).map_err(|_| {
                    tracing::error!(platform = "unix", "read length conversion overflowed");
                    PumpError::LengthOverflow
                })?;
                tracing::debug!(bytes = len, platform = "unix", "read stream chunk");
                return Ok(len);
            }
            Err(err) if err.kind() == io::ErrorKind::Interrupted => {
                tracing::warn!(platform = "unix", "retrying interrupted read (EINTR)");
                record_read_retry();
            }
            Err(err) => {
                tracing::error!(platform = "unix", error = %err, "fatal stream read failure");
                return Err(PumpError::from(err));
            }
        }
    }
}

#[cfg(unix)]
fn write_all_unix(writer: &StreamHandle, chunk: &[u8]) -> Result<WriteOutcome, PumpError> {
    let fd = writer.as_raw_fd();
    write_all_unix_with(chunk, |buffer| {
        // SAFETY: `buffer` is valid for reads of `buffer.len()` bytes, and
        // `fd` stays valid for the duration of this call.
        let written =
            unsafe { libc::write(fd, buffer.as_ptr().cast::<libc::c_void>(), buffer.len()) };
        if written >= 0 {
            Ok(written)
        } else {
            Err(io::Error::last_os_error())
        }
    })
}

/// Drive the partial-write loop over an injectable single-write operation.
///
/// `write_once` receives the still-unwritten tail and returns the number of
/// bytes accepted (never negative) or the raw `io::Error`. This mirrors
/// [`read_raw_fd_with`] so the write policy — `EINTR` retries, zero-progress
/// detection, and non-fatal broken-pipe short writes — can be verified without
/// real descriptors.
#[cfg(unix)]
fn write_all_unix_with(
    mut chunk: &[u8],
    mut write_once: impl FnMut(&[u8]) -> Result<libc::ssize_t, io::Error>,
) -> Result<WriteOutcome, PumpError> {
    let mut total_written = 0_u64;

    while !chunk.is_empty() {
        match write_once(chunk) {
            Ok(written) if written > 0 => {
                let written_len = usize::try_from(written).map_err(|_| {
                    tracing::error!(platform = "unix", "write length conversion overflowed");
                    PumpError::LengthOverflow
                })?;
                tracing::debug!(bytes = written_len, platform = "unix", "wrote stream chunk");
                record_write_progress(&mut chunk, written_len, &mut total_written)?;
            }
            Ok(_) => {
                tracing::error!(platform = "unix", "write made zero progress");
                return Err(PumpError::from(io::Error::new(
                    io::ErrorKind::WriteZero,
                    "failed to write whole buffer",
                )));
            }
            Err(err) if err.kind() == io::ErrorKind::Interrupted => {
                tracing::warn!(platform = "unix", "retrying interrupted write (EINTR)");
                record_write_retry();
            }
            Err(err) => return map_short_write_error(err, total_written),
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
    #[cfg(unix)]
    let platform = "unix";
    #[cfg(windows)]
    let platform = "windows";
    tracing::error!(platform, error = %pump_error, "fatal stream write failure");
    Err(pump_error)
}

#[cfg(all(test, unix))]
mod tests;

#[cfg(all(test, unix))]
mod tracing_tests;
