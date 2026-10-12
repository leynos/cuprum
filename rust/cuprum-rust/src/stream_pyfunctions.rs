//! Contain the Python stream exports and their generated `PyO3` wrappers.

use super::{
    BufferSize,
    PlatformFd,
    PumpError,
    PyErr,
    PyResult,
    Python,
    ReaderFd,
    RustStreamError,
    consume_stream,
    convert_fd,
    pump_stream,
    pyfunction,
    validate_buffer_size,
};

/// Run a prepared stream operation after validating its buffer size.
///
/// Keep the common `PyO3` boundary behaviour in one place: validate the
/// Python argument, prepare descriptor ownership, release the GIL for I/O,
/// and convert the typed boundary error into the exported Python exception.
///
/// The inner closure performs the existing sequence and yields the typed
/// error, so the one conversion below happens exactly once and after the GIL
/// has been reacquired. The detached operation itself keeps its `PumpError`
/// result: it outlives the argument checks and has no business knowing about
/// them, so only its error is classified on the way out.
fn run_stream_operation<T, Operation>(
    py: Python<'_>,
    reader_fd: i64,
    buffer_size: i64,
    prepare_operation: impl FnOnce() -> Result<Operation, RustStreamError>,
) -> PyResult<T>
where
    T: Send,
    Operation: FnOnce(ReaderFd, BufferSize) -> Result<T, PumpError> + Send,
{
    let result: Result<T, RustStreamError> = (|| {
        let validated_buffer_size = validate_buffer_size(buffer_size)?;
        let reader = ReaderFd(convert_fd(reader_fd)?);
        let operation = prepare_operation()?;
        py.detach(move || operation(reader, validated_buffer_size))
            .map_err(RustStreamError::Stream)
    })();
    result.map_err(PyErr::from)
}

/// Pump bytes between file descriptors outside the GIL.
///
/// # Parameters
/// - `reader_fd`: File descriptor for the upstream stdout.
/// - `writer_fd`: File descriptor for the downstream stdin.
/// - `buffer_size`: Size of the internal transfer buffer in bytes.
///
/// # Errors
/// Returns a Python `ValueError` for invalid buffer sizes and `OSError` for
/// I/O failures.
#[pyfunction]
#[pyo3(signature = (reader_fd, writer_fd, buffer_size = 65536))]
pub(super) fn rust_pump_stream(
    py: Python<'_>,
    reader_fd: i64,
    writer_fd: i64,
    buffer_size: i64,
) -> PyResult<u64> {
    run_stream_operation(py, reader_fd, buffer_size, || {
        let writer_raw: PlatformFd = convert_fd(writer_fd)?;
        // SAFETY: `_streams_rs` transfers its duplicate exactly once.
        // Python retains no owner after this hand-off; see the boundary
        // contract for direct native callers and submission rollback.
        let writer = unsafe { cuprum_native_io::adopt_writer(writer_raw) };
        Ok(move |reader: ReaderFd, validated_buffer_size| {
            // SAFETY: Python keeps the paused reader transport alive until
            // the worker and cleanup finish, including cancellation.
            let source = unsafe { cuprum_native_io::borrow_reader(reader.0) };
            pump_stream(&source, writer, validated_buffer_size)
        })
    })
}

/// Consume a stream and decode it as UTF-8 with replacement semantics.
///
/// This helper always uses UTF-8 and replaces invalid sequences with the
/// Unicode replacement character.
///
/// # Parameters
/// - `reader_fd`: File descriptor to read from.
/// - `buffer_size`: Size of the internal read buffer in bytes.
///
/// # Returns
/// The decoded stream content.
///
/// # Errors
/// Returns a Python `ValueError` for invalid arguments and `OSError` for
/// I/O failures.
#[pyfunction]
#[pyo3(signature = (reader_fd, buffer_size = 65536))]
pub(super) fn rust_consume_stream(
    py: Python<'_>,
    reader_fd: i64,
    buffer_size: i64,
) -> PyResult<String> {
    run_stream_operation(py, reader_fd, buffer_size, || {
        Ok(|reader: ReaderFd, size| {
            // SAFETY: the Python consume caller retains its reader throughout
            // this synchronous native call, including the GIL-free interval.
            let source = unsafe { cuprum_native_io::borrow_reader(reader.0) };
            consume_stream(&source, size)
        })
    })
}
