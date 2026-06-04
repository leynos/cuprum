//! I/O utilities for stream operations.
//!
//! This module provides helpers for reading and writing descriptor-backed
//! streams with proper error handling, including detection of non-fatal write
//! errors like broken pipes.

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
            Ok(true)
        }
        Err(err) => {
            if is_nonfatal_write_error(&err) {
                Ok(false)
            } else {
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
            return usize::try_from(read_len).map_err(|_| io::Error::other("read length overflow"));
        }

        let err = io::Error::last_os_error();
        if err.kind() != io::ErrorKind::Interrupted {
            return Err(err);
        }
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
            chunk = chunk
                .get(written_len..)
                .ok_or_else(|| io::Error::other("write length exceeded buffer"))?;
            continue;
        }

        if written == 0 {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "failed to write whole buffer",
            ));
        }

        let err = io::Error::last_os_error();
        if err.kind() != io::ErrorKind::Interrupted {
            return Err(err);
        }
    }

    Ok(())
}
