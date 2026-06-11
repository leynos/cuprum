//! Optional Rust extension for Cuprum stream operations.
//!
//! This crate exposes a `PyO3` module providing stream pump and consume
//! helpers alongside an availability check for the Rust backend.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[cfg(unix)]
use std::os::fd::{FromRawFd, OwnedFd};

#[cfg(windows)]
use std::fs::File;

#[cfg(windows)]
use std::os::windows::io::{FromRawHandle, RawHandle};

mod io_utils;
#[cfg(target_os = "linux")]
mod splice;
mod utf8;

use errors::PumpError;
use io_utils::{StreamHandle, handle_write_result, read_stream};
use utf8::{FinalChunk, decode_utf8_replace};

/// Report whether the Rust extension is available.
///
/// This entry point is only reached once the native module has loaded, so it
/// always reports `true`. The Python wrapper treats a failed import as
/// "unavailable", so no runtime probing is needed here.
///
/// # Returns
/// `true` whenever the extension is loaded and callable.
#[pyfunction]
const fn is_available() -> bool {
    true
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
fn rust_pump_stream(
    py: Python<'_>,
    reader_fd: i64,
    writer_fd: i64,
    buffer_size: i64,
) -> PyResult<u64> {
    let validated_buffer_size = validate_buffer_size(buffer_size)?;

    let reader = ReaderFd(convert_fd(reader_fd)?);
    let writer = WriterFd(convert_fd(writer_fd)?);

    let result = py.detach(|| pump_stream(reader, writer, validated_buffer_size));
    result.map_err(PyErr::from)
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
fn rust_consume_stream(py: Python<'_>, reader_fd: i64, buffer_size: i64) -> PyResult<String> {
    let validated_buffer_size = validate_buffer_size(buffer_size)?;

    let reader = ReaderFd(convert_fd(reader_fd)?);
    let result = py.detach(|| consume_stream(reader, validated_buffer_size));
    result.map_err(PyErr::from)
}

#[cfg(unix)]
fn convert_fd(value: i64) -> PyResult<PlatformFd> {
    let fd =
        i32::try_from(value).map_err(|_| PyValueError::new_err("file descriptor out of range"))?;
    if fd < 0 {
        return Err(PyValueError::new_err(
            "file descriptor must be non-negative",
        ));
    }
    Ok(fd)
}

#[cfg(windows)]
fn convert_fd(value: i64) -> PyResult<PlatformFd> {
    let handle_value = value as u64;
    if usize::BITS >= 64 {
        return Ok(handle_value as usize);
    }
    let truncated = u32::try_from(handle_value)
        .map_err(|_| PyValueError::new_err("file handle out of range"))?;
    Ok(truncated as usize)
}

/// Validate that `buffer_size` is positive and fits into a usize.
///
/// # Errors
/// Returns `PyValueError` if `buffer_size` is non-positive or out of range.
fn validate_buffer_size(buffer_size: i64) -> PyResult<BufferSize> {
    if buffer_size <= 0 {
        return Err(PyValueError::new_err(
            "buffer_size must be greater than zero",
        ));
    }
    usize::try_from(buffer_size)
        .map(BufferSize)
        .map_err(|_| PyValueError::new_err("buffer_size is too large"))
}

#[cfg(unix)]
fn stream_from_raw(fd: PlatformFd) -> StreamHandle {
    // SAFETY: The caller ensures the fd is valid and owned by the caller.
    unsafe { OwnedFd::from_raw_fd(fd) }
}

#[cfg(windows)]
fn stream_from_raw(handle: PlatformFd) -> StreamHandle {
    // SAFETY: The caller ensures the handle is valid and owned by the caller.
    unsafe { File::from_raw_handle(handle as RawHandle) }
}

/// Pump bytes between file descriptors with explicit ownership semantics.
///
/// The reader FD is borrowed and left open. The writer FD is treated as
/// consumed and closes on drop to signal EOF downstream.
fn pump_stream(
    reader_fd: ReaderFd,
    writer_fd: WriterFd,
    buffer_size: BufferSize,
) -> Result<u64, PumpError> {
    let mut reader = stream_from_raw(reader_fd.0);
    let mut writer = stream_from_raw(writer_fd.0);
    let result = pump_stream_files(&mut reader, &mut writer, buffer_size);
    // The caller owns the reader FD; avoid closing it here.
    // The writer FD is treated as consumed and closes on drop to signal EOF.
    std::mem::forget(reader);
    result
}

/// Consume bytes from a file descriptor and decode UTF-8 with replacement.
fn consume_stream(reader_fd: ReaderFd, buffer_size: BufferSize) -> Result<String, PumpError> {
    let mut reader = stream_from_raw(reader_fd.0);
    let result = consume_stream_files(&mut reader, buffer_size);
    // The caller owns the reader FD; avoid closing it here.
    std::mem::forget(reader);
    result
}

#[cfg(unix)]
type PlatformFd = i32;

#[cfg(windows)]
type PlatformFd = usize;

#[derive(Clone, Copy, Debug)]
struct ReaderFd(PlatformFd);

#[derive(Clone, Copy, Debug)]
struct WriterFd(PlatformFd);

#[derive(Clone, Copy, Debug)]
struct BufferSize(usize);

impl BufferSize {
    const fn value(self) -> usize {
        self.0
    }
}

fn pump_stream_files(
    reader: &mut StreamHandle,
    writer: &mut StreamHandle,
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
    reader: &mut StreamHandle,
    writer: &mut StreamHandle,
    buffer_size: BufferSize,
) -> Result<u64, PumpError> {
    let mut buffer = vec![0_u8; buffer_size.value()];
    let mut total_written = 0_u64;
    let mut writer_open = true;

    loop {
        let read_len = read_stream(reader, &mut buffer)?;
        if read_len == 0 {
            break;
        }
        if !writer_open {
            continue;
        }

        let chunk = buffer
            .get(..read_len)
            .ok_or(PumpError::BufferRangeExceeded)?;

        writer_open = handle_write_result(writer, chunk, &mut total_written)?;
    }

    Ok(total_written)
}

fn consume_stream_files(
    reader: &mut StreamHandle,
    buffer_size: BufferSize,
) -> Result<String, PumpError> {
    let mut buffer = vec![0_u8; buffer_size.value()];
    let mut pending: Vec<u8> = Vec::new();
    let mut output = String::new();

    loop {
        let read_len = read_stream(reader, &mut buffer)?;
        if read_len == 0 {
            break;
        }
        let chunk = buffer
            .get(..read_len)
            .ok_or(PumpError::BufferRangeExceeded)?;
        pending.extend_from_slice(chunk);
        decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(false));
    }

    decode_utf8_replace(&mut pending, &mut output, FinalChunk::new(true));

    Ok(output)
}

/// Python module definition for the optional Rust backend.
///
/// # Errors
/// Returns a Python error if the module cannot be initialized.
#[pymodule]
fn _rust_backend_native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(is_available, module)?)?;
    module.add_function(wrap_pyfunction!(rust_pump_stream, module)?)?;
    module.add_function(wrap_pyfunction!(rust_consume_stream, module)?)?;
    module.add("__doc__", "Cuprum optional Rust backend.")?;
    module.add("__package__", "cuprum")?;
    module.add("__loader__", py.None())?;
    Ok(())
}

mod errors;
