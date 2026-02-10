//! I/O utilities for stream operations.
//!
//! This module provides helpers for writing to files with proper error
//! handling, including detection of non-fatal write errors like broken pipes.

use std::fs::File;
use std::io::{self, Write};

/// Write all bytes from a chunk to the writer, returning bytes written.
pub(crate) fn handle_write(writer: &mut File, chunk: &[u8]) -> Result<u64, io::Error> {
    writer.write_all(chunk)?;
    u64::try_from(chunk.len())
        .map_err(|_| io::Error::new(io::ErrorKind::Other, "write length overflow"))
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
    writer: &mut File,
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
