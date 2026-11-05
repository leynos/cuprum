//! Windows `PyO3` stream exports with separate pump-handle and consume-CRT
//! boundaries.

use std::os::windows::io::{FromRawHandle, RawHandle};

use cuprum_native_io::OwnedStream;
use pyo3::exceptions::PyOSError;

use super::{
    PyResult,
    Python,
    RustStreamError,
    consume_crt_stream,
    convert_fd,
    pyfunction,
    validate_buffer_size,
};

fn raw_windows_handle_error<T>() -> PyResult<T> {
    Err(PyOSError::new_err(
        "native stream operations do not accept raw Windows handles; use the Python fallback",
    ))
}

fn reject_transferred_windows_writer<T>(writer: OwnedStream) -> PyResult<T> {
    drop(writer);
    raw_windows_handle_error()
}

/// Validate the arguments for pumping bytes between Windows handles.
///
/// Windows handles cannot be safely treated as synchronous streams at this
/// boundary, so the function rejects the operation with `OSError` instead
/// of pumping data. Once the writer handle has been adopted, it is closed on
/// rejection and if later argument validation fails.
///
/// # Parameters
/// - `reader_fd`: Windows handle for the upstream stdout.
/// - `writer_fd`: Windows handle for the downstream stdin.
/// - `buffer_size`: Size of the internal transfer buffer in bytes.
///
/// # Errors
/// Returns a Python `ValueError` for invalid arguments and `OSError` when the
/// Windows raw-handle operation is rejected.
#[pyfunction]
#[pyo3(signature = (reader_fd, writer_fd, buffer_size = 65536))]
pub(super) fn rust_pump_stream(
    _py: Python<'_>,
    reader_fd: i64,
    writer_fd: i64,
    buffer_size: i64,
) -> PyResult<u64> {
    let writer_raw = convert_fd(writer_fd)?;
    // SAFETY: this PyO3 entry point accepts a valid, uniquely owned Windows
    // writer transfer. Generic ownership does not assert that the rejected
    // handle supports synchronous I/O.
    let writer = unsafe { OwnedStream::from_raw_handle(writer_raw as RawHandle) };
    validate_buffer_size(buffer_size)?;
    convert_fd(reader_fd)?;
    reject_transferred_windows_writer(writer)
}

/// Validate the arguments for consuming a Windows CRT file descriptor.
///
/// # Parameters
/// - `reader_fd`: borrowed Windows CRT descriptor to read from.
/// - `buffer_size`: Size of the internal read buffer in bytes.
///
/// # Errors
/// Returns a Python `ValueError` for invalid arguments and `OSError` for CRT
/// read failures.
#[pyfunction]
#[pyo3(signature = (reader_fd, buffer_size = 65536))]
pub(super) fn rust_consume_stream(
    py: Python<'_>,
    reader_fd: i64,
    buffer_size: i64,
) -> PyResult<String> {
    let size = validate_buffer_size(buffer_size)?;
    let raw = convert_crt_fd(reader_fd)?;
    // SAFETY: the wrapper owns a duplicate CRT descriptor until this detached
    // operation returns, and sets that descriptor to binary mode first.
    let reader = unsafe { cuprum_native_io::borrow_crt_reader(raw) };
    py.detach(move || consume_crt_stream(reader, size))
        .map_err(RustStreamError::Stream)
        .map_err(Into::into)
}

fn convert_crt_fd(value: i64) -> Result<i32, RustStreamError> {
    let descriptor = i32::try_from(value)
        .map_err(|_| RustStreamError::InvalidDescriptor("file descriptor out of range"))?;
    if descriptor < 0 {
        return Err(RustStreamError::InvalidDescriptor(
            "file descriptor must be non-negative",
        ));
    }
    Ok(descriptor)
}
