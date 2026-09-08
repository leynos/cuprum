//! Contain the Python stream exports and their generated `PyO3` wrappers.

use super::{
    BufferSize, PumpError, PyErr, PyResult, Python, ReaderFd, WriterFd, consume_stream, convert_fd,
    pump_stream, pyfunction, validate_buffer_size,
};

/// Run a prepared stream operation after validating its buffer size.
///
/// Keep the common `PyO3` boundary behaviour in one place: validate the
/// Python argument, prepare descriptor ownership, release the GIL for I/O,
/// and map `PumpError` back to the exported Python exception type.
fn run_stream_operation<T, Operation>(
    py: Python<'_>,
    reader_fd: i64,
    buffer_size: i64,
    prepare_operation: impl FnOnce() -> PyResult<Operation>,
) -> PyResult<T>
where
    T: Send,
    Operation: FnOnce(ReaderFd, BufferSize) -> Result<T, PumpError> + Send,
{
    let validated_buffer_size = validate_buffer_size(buffer_size)?;
    let reader = ReaderFd(convert_fd(reader_fd)?);
    let operation = prepare_operation()?;
    let result = py.detach(move || operation(reader, validated_buffer_size));
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
        let writer = WriterFd(convert_fd(writer_fd)?);
        Ok(move |reader, validated_buffer_size| pump_stream(reader, writer, validated_buffer_size))
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
    run_stream_operation(py, reader_fd, buffer_size, || Ok(consume_stream))
}
