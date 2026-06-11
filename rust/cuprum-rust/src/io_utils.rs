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

/// Write all bytes from a chunk to the writer, returning bytes written.
pub(crate) fn handle_write(writer: &mut StreamHandle, chunk: &[u8]) -> Result<u64, PumpError> {
    #[cfg(unix)]
    write_all_unix(writer, chunk)?;

    #[cfg(windows)]
    writer.write_all(chunk).map_err(PumpError::from)?;

    u64::try_from(chunk.len()).map_err(|_| PumpError::LengthOverflow)
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
        Ok(bytes) => {
            *total_written = total_written.saturating_add(bytes);
            Ok(true)
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
fn write_all_unix(writer: &StreamHandle, mut chunk: &[u8]) -> Result<(), PumpError> {
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
            chunk = chunk
                .get(written_len..)
                .ok_or(PumpError::BufferRangeExceeded)?;
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
            return Err(PumpError::from(err));
        }
    }

    Ok(())
}
