//! Decode a borrowed stream as UTF-8 with replacement semantics.
//!
//! Each platform supplies its own entry point so this module never widens the
//! Windows synchronous-handle capability, while the crate root keeps a single
//! `consume_stream` name. [`consume_with_reader`] is the shared loop over a
//! read closure and the seam the crate's tests drive directly.

#[cfg(unix)]
use cuprum_native_io::AsStream;
#[cfg(windows)]
use cuprum_native_io::SynchronousBorrowedStream;

use crate::{
    BufferSize,
    buffer::allocate_buffer,
    errors::PumpError,
    io_utils::{operation_span, read_retry_count, read_stream, reset_retry_counters},
    utf8::{FinalChunk, decode_utf8_replace},
};

/// Decode a borrowed stream as UTF-8 with replacement semantics.
///
/// # Errors
/// Returns a semantic I/O, buffer-range, or accounting failure.
#[cfg(unix)]
pub fn consume_stream(
    reader: &impl AsStream,
    buffer_size: BufferSize,
) -> Result<String, PumpError> {
    consume_stream_files(reader, buffer_size)
}

/// Decode a synchronous Windows stream as UTF-8 with replacement semantics.
///
/// # Errors
/// Returns a semantic I/O, buffer-range, or accounting failure.
#[cfg(windows)]
pub fn consume_stream(
    reader: SynchronousBorrowedStream<'_>,
    buffer_size: BufferSize,
) -> Result<String, PumpError> {
    consume_stream_files(reader, buffer_size)
}

#[cfg(unix)]
pub(crate) fn consume_stream_files(
    reader: &impl AsStream,
    buffer_size: BufferSize,
) -> Result<String, PumpError> {
    consume_with_reader(|buffer| read_stream(reader, buffer), buffer_size, "unix")
}

#[cfg(windows)]
pub(crate) fn consume_stream_files(
    reader: SynchronousBorrowedStream<'_>,
    buffer_size: BufferSize,
) -> Result<String, PumpError> {
    consume_with_reader(|buffer| read_stream(reader, buffer), buffer_size, "windows")
}

/// Widen a read length for the byte counter, logging and failing on overflow.
///
/// `platform` labels the overflow event so it names the entry point's
/// platform.
fn read_length_as_u64(read_len: usize, platform: &'static str) -> Result<u64, PumpError> {
    u64::try_from(read_len).map_err(|_| {
        tracing::error!(platform, "read length conversion overflowed");
        PumpError::LengthOverflow
    })
}

/// Borrow the `read_len` bytes a read filled, failing if the reader reported
/// more than the buffer holds.
fn filled_prefix(buffer: &[u8], read_len: usize) -> Result<&[u8], PumpError> {
    buffer.get(..read_len).ok_or(PumpError::BufferRangeExceeded)
}

/// Perform one read, mapping end of stream (a zero-length read) to `None`.
fn read_nonempty(
    read: &mut impl FnMut(&mut [u8]) -> Result<usize, PumpError>,
    buffer: &mut [u8],
) -> Result<Option<usize>, PumpError> {
    let read_len = read(buffer)?;
    Ok((read_len != 0).then_some(read_len))
}

/// Decoded text so far, plus any incomplete trailing UTF-8 sequence.
#[derive(Default)]
struct DecodeState {
    pending: Vec<u8>,
    output: String,
}

/// Decode the first `read_len` bytes of `buffer` into `state`. Returns the
/// number of bytes absorbed.
fn absorb_read(
    buffer: &[u8],
    read_len: usize,
    platform: &'static str,
    state: &mut DecodeState,
) -> Result<u64, PumpError> {
    let read_bytes = read_length_as_u64(read_len, platform)?;
    state
        .pending
        .extend_from_slice(filled_prefix(buffer, read_len)?);
    decode_utf8_replace(
        &mut state.pending,
        &mut state.output,
        FinalChunk::new(false),
    );
    Ok(read_bytes)
}

/// Drain `read` to EOF, decoding the bytes as UTF-8 with replacement.
///
/// Each platform entry point supplies a closure over its own capability-typed
/// `read_stream`, so this loop never sees a handle and cannot widen the
/// Windows synchronous-handle boundary. `platform` labels the length-overflow
/// event so it names the entry point's platform.
pub(crate) fn consume_with_reader(
    mut read: impl FnMut(&mut [u8]) -> Result<usize, PumpError>,
    buffer_size: BufferSize,
    platform: &'static str,
) -> Result<String, PumpError> {
    // Operation span (see `operation_span`) so the read seam's `warn!`/`error!`
    // events inherit this operation's context even under a `warn`/`error`-only
    // production filter.
    let span = operation_span("consume_stream", buffer_size.value());
    let _guard = span.enter();
    reset_retry_counters();

    let mut buffer = allocate_buffer(buffer_size.value())?;
    let mut state = DecodeState::default();
    let mut total_read = 0_u64;

    while let Some(read_len) = read_nonempty(&mut read, &mut buffer)? {
        let absorbed = absorb_read(&buffer, read_len, platform, &mut state)?;
        total_read = total_read.saturating_add(absorbed);
    }

    decode_utf8_replace(&mut state.pending, &mut state.output, FinalChunk::new(true));

    span.record("total_bytes", total_read);
    span.record("read_retries", read_retry_count());
    Ok(state.output)
}
