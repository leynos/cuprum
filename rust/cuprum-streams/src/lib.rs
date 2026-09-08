//! Safe stream orchestration, decoding, and error policy for Cuprum.
#![forbid(unsafe_code)]

#[cfg(test)]
mod buffer_size_tests;
#[cfg(all(test, unix))]
mod consume_snapshot_tests;
mod errors;
mod io_utils;
#[cfg(all(test, unix))]
mod lib_tests;
mod pump_machine;
#[cfg(target_os = "linux")]
mod splice;
#[cfg(all(test, unix))]
mod test_support;
#[cfg(all(test, unix))]
mod tracing_capture;
mod utf8;

use cuprum_native_io::{AsStream, OwnedStream};
pub use errors::PumpError;
use io_utils::{classify_write, operation_span, read_stream};
use pump_machine::{Flow, PumpState, advance};
use utf8::{FinalChunk, decode_utf8_replace};

/// Maximum accepted stream buffer size, in bytes (1 GiB).
///
/// This guards against absurd allocations from a bad `buffer_size` while
/// comfortably exceeding any realistic transfer buffer — the default is
/// 64 KiB and even multi-megabyte buffers stay far below this cap.
const MAX_BUFFER_SIZE: usize = 1 << 30;

/// Validate and convert a raw `buffer_size` into a bounded `usize`.
///
/// This is the pure decision core behind [`validate_buffer_size`]: it takes no
/// Python state and returns a stable, message-carrying error so it can be
/// property tested directly.
///
/// # Errors
/// Returns a stable error message when `buffer_size` is non-positive, overflows
/// `usize` on the target platform, or exceeds [`MAX_BUFFER_SIZE`].
fn checked_buffer_size(buffer_size: i64) -> Result<usize, &'static str> {
    if buffer_size <= 0 {
        return Err("buffer_size must be greater than zero");
    }
    let size = usize::try_from(buffer_size).map_err(|_| "buffer_size is too large")?;
    if size > MAX_BUFFER_SIZE {
        return Err("buffer_size exceeds the maximum permitted size");
    }
    Ok(size)
}

/// Validated positive buffer allocation size, capped at 1 GiB.
#[derive(Clone, Copy, Debug)]
pub struct BufferSize(usize);

impl BufferSize {
    /// Validate a buffer size supplied by the integration layer.
    ///
    /// # Errors
    /// Rejects non-positive sizes and sizes above the allocation cap.
    pub fn new(size: i64) -> Result<Self, &'static str> {
        checked_buffer_size(size).map(Self)
    }

    const fn value(self) -> usize {
        self.0
    }
}

/// Pump from a borrowed reader, consuming the uniquely owned writer.
///
/// The writer drops on success, error, and unwind. The reader's owner must
/// outlive this borrow and remains responsible for closing it.
///
/// # Errors
/// Returns a semantic I/O, buffer-range, or accounting failure.
pub fn pump_stream(
    reader: &impl AsStream,
    writer: OwnedStream,
    buffer_size: BufferSize,
) -> Result<u64, PumpError> {
    cuprum_native_io::with_owned_writer(reader, writer, |source, sink| {
        pump_stream_files(source, sink, buffer_size)
    })
}

/// Decode a borrowed stream as UTF-8 with replacement semantics.
///
/// # Errors
/// Returns a semantic I/O, buffer-range, or accounting failure.
pub fn consume_stream(
    reader: &impl AsStream,
    buffer_size: BufferSize,
) -> Result<String, PumpError> {
    consume_stream_files(reader, buffer_size)
}

fn pump_stream_files(
    reader: &impl AsStream,
    writer: &impl AsStream,
    buffer_size: BufferSize,
) -> Result<u64, PumpError> {
    // On Linux, attempt zero-copy splice first.
    #[cfg(target_os = "linux")]
    if let Some(result) = splice::try_splice_pump(reader, writer, buffer_size.value()) {
        return result;
    }

    // Fallback: read/write loop for non-Linux or unsupported FD types.
    pump_stream_files_readwrite(reader, writer, buffer_size)
}

/// Read/write loop fallback for pumping bytes between file descriptors.
///
/// This is used when splice is not available (non-Linux) or when the file
/// descriptors do not support splice (regular files, some sockets).
fn pump_stream_files_readwrite(
    reader: &impl AsStream,
    writer: &impl AsStream,
    buffer_size: BufferSize,
) -> Result<u64, PumpError> {
    // Operation span (see `operation_span`) so the EINTR (`warn!`) and
    // fatal-I/O (`error!`) events emitted from the read/write seams inherit
    // the operation name, `buffer_size`, and `total_bytes` context even under
    // a `warn`/`error`-only production filter.
    let span = operation_span("pump_stream_readwrite", buffer_size.value());
    let _guard = span.enter();
    io_utils::reset_retry_counters();

    let mut buffer = vec![0_u8; buffer_size.value()];
    let mut state = PumpState::start();

    loop {
        let read_len = read_stream(reader, &mut buffer)?;
        let writer_was_open = state.writer_open();

        // `advance` owns both the zero-length-is-EOF translation and the write
        // precondition — a chunk read while the writer is still open — so this
        // loop, the property tests, and the bounded proofs share one
        // definition of them. Fatal writes propagate the real error and never
        // reach the pure state machine.
        let flow = advance(&mut state, read_len, || {
            let chunk = buffer
                .get(..read_len)
                .ok_or(PumpError::BufferRangeExceeded)?;
            classify_write(writer, chunk)
        })?;

        // The latch closing is the `head`-style early exit. Mirror splice's
        // field and message so the event is not visible on one path only, and
        // observe it here rather than in the deliberately pure `pump_machine`.
        if writer_was_open && !state.writer_open() {
            tracing::debug!(
                bytes_transferred = state.total_written(),
                "broken pipe; draining reader"
            );
        }

        if flow == Flow::Stop {
            break;
        }
    }

    let total_written = state.total_written();
    span.record("total_bytes", total_written);
    span.record("read_retries", io_utils::read_retry_count());
    span.record("write_retries", io_utils::write_retry_count());
    Ok(total_written)
}

fn consume_stream_files(
    reader: &impl AsStream,
    buffer_size: BufferSize,
) -> Result<String, PumpError> {
    // Operation span (see `operation_span`) so the read seam's `warn!`/`error!`
    // events inherit this operation's context even under a `warn`/`error`-only
    // production filter.
    let span = operation_span("consume_stream", buffer_size.value());
    let _guard = span.enter();
    io_utils::reset_retry_counters();

    let mut buffer = vec![0_u8; buffer_size.value()];
    let mut pending: Vec<u8> = Vec::new();
    let mut output = String::new();
    let mut total_read = 0_u64;

    #[cfg(unix)]
    let platform = "unix";
    #[cfg(windows)]
    let platform = "windows";

    loop {
        let read_len = read_stream(reader, &mut buffer)?;
        if read_len == 0 {
            break;
        }
        let read_bytes = u64::try_from(read_len).map_err(|_| {
            tracing::error!(platform, "read length conversion overflowed");
            PumpError::LengthOverflow
        })?;
        total_read = total_read.saturating_add(read_bytes);
        let chunk = buffer
            .get(..read_len)
            .ok_or(PumpError::BufferRangeExceeded)?;
        pending.extend_from_slice(chunk);
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(false));
    }

    decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(true));

    span.record("total_bytes", total_read);
    span.record("read_retries", io_utils::read_retry_count());
    Ok(output)
}
