//! Optional Rust extension for Cuprum stream operations.
//!
//! This crate exposes a minimal PyO3 module that allows Python to detect
//! whether the Rust extension is available. The core stream operations are
//! implemented later in the roadmap.

use std::fs::File;
use std::io::{self, Read, Write};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[cfg(unix)]
use std::os::fd::FromRawFd;

#[cfg(windows)]
use std::os::windows::io::{FromRawHandle, RawHandle};

/// Report whether the Rust extension is available.
///
/// # Returns
/// `true` when the extension is successfully loaded.
#[pyfunction]
#[must_use]
fn is_available(_py: Python<'_>) -> PyResult<bool> {
    Ok(true)
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
#[must_use]
fn rust_pump_stream(
    py: Python<'_>,
    reader_fd: i64,
    writer_fd: i64,
    buffer_size: usize,
) -> PyResult<u64> {
    if buffer_size == 0 {
        return Err(PyValueError::new_err(
            "buffer_size must be greater than zero",
        ));
    }

    let reader_fd = convert_fd(reader_fd)?;
    let writer_fd = convert_fd(writer_fd)?;

    let result = py.detach(|| pump_stream(reader_fd, writer_fd, buffer_size));
    result.map_err(PyErr::from)
}

#[cfg(unix)]
#[must_use]
fn convert_fd(value: i64) -> PyResult<PlatformFd> {
    let fd = i32::try_from(value)
        .map_err(|_| PyValueError::new_err("file descriptor out of range"))?;
    if fd < 0 {
        return Err(PyValueError::new_err("file descriptor must be non-negative"));
    }
    Ok(fd)
}

#[cfg(windows)]
#[must_use]
fn convert_fd(value: i64) -> PyResult<PlatformFd> {
    let handle_value = value as u64;
    if usize::BITS >= 64 {
        return Ok(handle_value as usize);
    }
    let truncated = u32::try_from(handle_value)
        .map_err(|_| PyValueError::new_err("file handle out of range"))?;
    Ok(truncated as usize)
}

#[cfg(unix)]
fn file_from_raw(fd: PlatformFd) -> File {
    // SAFETY: The caller ensures the fd is valid and owned by the caller.
    unsafe { File::from_raw_fd(fd) }
}

#[cfg(windows)]
fn file_from_raw(handle: PlatformFd) -> File {
    // SAFETY: The caller ensures the handle is valid and owned by the caller.
    unsafe { File::from_raw_handle(handle as RawHandle) }
}

fn pump_stream(
    reader_fd: PlatformFd,
    writer_fd: PlatformFd,
    buffer_size: usize,
) -> Result<u64, io::Error> {
    let mut reader = file_from_raw(reader_fd);
    let mut writer = file_from_raw(writer_fd);
    let result = pump_stream_files(&mut reader, &mut writer, buffer_size);
    // The caller owns the reader FD; avoid closing it here.
    // The writer FD is treated as consumed and closes on drop to signal EOF.
    std::mem::forget(reader);
    result
}

#[cfg(unix)]
type PlatformFd = i32;

#[cfg(windows)]
type PlatformFd = usize;

fn handle_write(writer: &mut File, chunk: &[u8]) -> Result<u64, io::Error> {
    writer.write_all(chunk)?;
    u64::try_from(chunk.len())
        .map_err(|_| io::Error::new(io::ErrorKind::Other, "write length overflow"))
}

fn handle_write_result(
    writer: &mut File,
    chunk: &[u8],
    total_written: &mut u64,
) -> Result<bool, io::Error> {
    match handle_write(writer, chunk) {
        Ok(bytes) => {
            *total_written = total_written.saturating_add(bytes);
            Ok(true)
        }
        Err(err)
            if matches!(
                err.kind(),
                io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset
            ) =>
        {
            Ok(false)
        }
        Err(err) => Err(err),
    }
}

fn pump_stream_files(
    reader: &mut File,
    writer: &mut File,
    buffer_size: usize,
) -> Result<u64, io::Error> {
    let mut buffer = vec![0_u8; buffer_size];
    let mut total_written = 0_u64;
    let mut writer_open = true;

    loop {
        let read_len = reader.read(&mut buffer)?;
        if read_len == 0 {
            break;
        }
        if !writer_open {
            continue;
        }

        let chunk = buffer.get(..read_len).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "read length exceeds buffer size",
            )
        })?;

        writer_open = handle_write_result(writer, chunk, &mut total_written)?;
    }

    Ok(total_written)
}

/// Python module definition for the optional Rust backend.
///
/// # Errors
/// Returns a Python error if the module cannot be initialized.
#[pymodule]
fn _rust_backend_native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(is_available, module)?)?;
    module.add_function(wrap_pyfunction!(rust_pump_stream, module)?)?;
    module.add("__doc__", "Cuprum optional Rust backend.")?;
    module.add("__package__", "cuprum")?;
    module.add("__loader__", py.None())?;
    Ok(())
}
