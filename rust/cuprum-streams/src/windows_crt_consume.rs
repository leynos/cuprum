//! Route borrowed Windows CRT descriptors through the shared UTF-8 decoder.

use cuprum_native_io::BorrowedCrtFileDescriptor;

use crate::{BufferSize, PumpError, consume_with_reader, io_utils::read_crt_stream};

/// Decode a borrowed Windows CRT descriptor as UTF-8 with replacement.
///
/// The descriptor must remain open and in binary mode for the duration of the
/// call. Python supplies a temporary duplicate so native reads cannot change
/// the caller's descriptor mode or ownership.
///
/// # Errors
/// Returns a semantic I/O, buffer-range, or accounting failure.
pub fn consume_crt_stream(
    reader: BorrowedCrtFileDescriptor<'_>,
    buffer_size: BufferSize,
) -> Result<String, PumpError> {
    consume_with_reader(
        |buffer| read_crt_stream(reader, buffer),
        buffer_size,
        "windows-crt",
    )
}
