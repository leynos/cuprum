//! Route owned Windows CRT descriptors through the shared UTF-8 decoder.
//!
//! This is the Windows counterpart of the generic consume path: the decode
//! loop, the buffer policy, and the error taxonomy are shared, and only the
//! single-read seam differs. [`consume_crt_stream`] calls
//! [`crate::io_utils::read_crt_stream`] once per chunk, and that adapter is the
//! only place a CRT descriptor is read — it forwards to
//! [`cuprum_native_io::read_crt_once`], which owns the `_read` call and its
//! error classification.
//!
//! The descriptor arrives as [`cuprum_native_io::BorrowedCrtFileDescriptor`],
//! a borrow that can only be obtained from an
//! [`cuprum_native_io::OwnedCrtFileDescriptor`]. Nothing here can therefore
//! close or invalidate the descriptor it reads from, and nothing here may
//! acquire ownership of it: this crate forbids `unsafe` code, so the whole
//! lifetime contract stays inside `cuprum-native-io`.

use cuprum_native_io::BorrowedCrtFileDescriptor;

use crate::{BufferSize, PumpError, consume_with_reader, io_utils::read_crt_stream};

/// Decode a borrowed Windows CRT descriptor as UTF-8 with replacement.
///
/// Ownership and binary mode are already established: the only way to obtain
/// the borrow is [`cuprum_native_io::OwnedCrtFileDescriptor::as_borrowed`],
/// whose owner keeps the descriptor open and in binary mode for as long as the
/// borrow lives. The caller's own descriptor is untouched, because the
/// duplicate the read uses is a separate number.
///
/// This pins the public signature the `cfg(windows)` re-export publishes, and
/// it names the same items the module documentation does so a renamed export
/// fails here rather than only on a Windows runner:
///
/// ```
/// use cuprum_native_io::{BorrowedCrtFileDescriptor, OwnedCrtFileDescriptor};
/// use cuprum_streams::{BufferSize, consume_crt_stream};
///
/// let _consume: for<'a> fn(
///     BorrowedCrtFileDescriptor<'a>,
///     BufferSize,
/// ) -> Result<String, cuprum_streams::PumpError> = consume_crt_stream;
/// let _lend: for<'a> fn(&'a OwnedCrtFileDescriptor) -> BorrowedCrtFileDescriptor<'a> =
///     OwnedCrtFileDescriptor::as_borrowed;
/// ```
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
