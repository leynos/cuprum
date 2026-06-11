//! Optional Rust extension for Cuprum stream operations.
//!
//! This crate exposes a `PyO3` module providing stream pump and consume
//! helpers alongside an availability check for the Rust backend.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use std::mem::ManuallyDrop;

#[cfg(unix)]
use std::os::fd::{FromRawFd, OwnedFd};

#[cfg(windows)]
use std::fs::File;

#[cfg(windows)]
use std::os::windows::io::{FromRawHandle, RawHandle};

mod errors;
#[cfg(test)]
mod fd_tests;
mod io_utils;
#[cfg(all(test, unix))]
mod lib_tests;
#[cfg(target_os = "linux")]
mod splice;
#[cfg(all(test, unix))]
mod test_support;
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
    convert_platform_fd(value).map_err(PyValueError::new_err)
}

#[cfg(unix)]
fn convert_platform_fd(value: i64) -> Result<PlatformFd, &'static str> {
    // Reject negative handles for symmetry with the Unix arm: Python hands
    // over non-negative handle values, and reinterpreting a negative i64 as
    // a pointer-sized handle would silently address nonsense.
    if value < 0 {
        return Err("file handle must be non-negative");
    }
    usize::try_from(value).map_err(|_| "file handle out of range")
}

#[cfg(windows)]
fn convert_platform_fd(value: i64) -> Result<PlatformFd, &'static str> {
    // Reject negative handles for symmetry with the Unix arm: Python hands
    // over non-negative handle values, and reinterpreting a negative i64 as
    // a pointer-sized handle would silently address nonsense.
    if value < 0 {
        return Err("file handle must be non-negative");
    }
    usize::try_from(value).map_err(|_| "file handle out of range")
}

#[cfg(windows)]
fn convert_fd(value: i64) -> PyResult<PlatformFd> {
    convert_platform_fd(value).map_err(PyValueError::new_err)
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
fn stream_from_raw(handle: PlatformFd) -> StreamHandle {
    // The usize-to-pointer cast is a deliberate, documented reinterpretation:
    // Windows handles are pointer-sized opaque values, so this widens or
    // narrows nothing.
    // SAFETY: The caller ensures the handle is valid and owned by the caller.
    unsafe { File::from_raw_handle(handle as RawHandle) }
}

/// Run `operation` against a `StreamHandle` borrowed from a caller-owned FD.
///
/// This is the canonical "borrow this FD without owning it" helper: the
/// handle is wrapped in [`ManuallyDrop`], so it is **never** closed when the
/// scope ends — including during unwinding from a panicking `operation`.
/// The previous pattern (a trailing `std::mem::forget` after the inner call)
/// was skipped on unwind, closing the caller-owned descriptor and exposing
/// the Python side to a double close.
///
/// There is deliberately no writer variant: the writer FD handed to
/// [`pump_stream`] is *consumed* — it must close (on drop, including during
/// unwinding) to signal EOF downstream.
///
/// SAFETY: the caller must guarantee that `fd` is a valid open descriptor
/// (or handle on Windows) for the duration of the call, and that ownership
/// remains with the caller; the helper guarantees it never closes `fd`.
fn with_borrowed_reader<T>(fd: PlatformFd, operation: impl FnOnce(&mut StreamHandle) -> T) -> T {
    let mut handle = ManuallyDrop::new(stream_from_raw(fd));
    // `ManuallyDrop` suppresses the close in every exit path, so no
    // drop-guard or forget call is required even if `operation` panics.
    operation(&mut handle)
}

#[cfg(windows)]
fn stream_from_raw(handle: PlatformFd) -> StreamHandle {
    // The usize-to-pointer cast is a deliberate, documented reinterpretation:
    // Windows handles are pointer-sized opaque values, so this widens or
    // narrows nothing.
    // SAFETY: The caller ensures the handle is valid and owned by the caller.
    unsafe { File::from_raw_handle(handle as RawHandle) }
}
/// Pump bytes between file descriptors with explicit ownership semantics.
///
/// The reader FD is borrowed and left open. The writer FD is treated as
/// consumed and closes on drop to signal EOF downstream — including when
/// the pump unwinds.
fn pump_stream(
    reader_fd: ReaderFd,
    writer_fd: WriterFd,
    buffer_size: BufferSize,
) -> Result<u64, PumpError> {
    let mut writer = stream_from_raw(writer_fd.0);
    with_borrowed_reader(reader_fd.0, |reader| {
        pump_stream_files(reader, &mut writer, buffer_size)
    })
}

/// Consume bytes from a file descriptor and decode UTF-8 with replacement.
fn consume_stream(reader_fd: ReaderFd, buffer_size: BufferSize) -> Result<String, PumpError> {
    with_borrowed_reader(reader_fd.0, |reader| {
        consume_stream_files(reader, buffer_size)
    })
}

#[cfg(unix)]
type PlatformFd = usize;

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

mod tests {
    //! Unit tests for the borrowed-FD ownership contract: the caller-owned
    //! reader descriptor stays open after normal completion and — critically
    //! — after a panicking inner operation (no close-on-unwind).

    use std::io::{self, Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::panic::{AssertUnwindSafe, catch_unwind};

    use super::with_borrowed_reader;

    fn make_pipe() -> (OwnedFd, OwnedFd) {
        let mut fds = [0_i32; 2];
        // SAFETY: `fds` is a valid two-element array for `pipe(2)` to fill.
        let rc = unsafe { libc::pipe(fds.as_mut_ptr()) };
        assert_eq!(rc, 0, "pipe(2) failed: {}", io::Error::last_os_error());
        // SAFETY: on success `pipe(2)` returned two freshly opened FDs that
        // this process exclusively owns.
        unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) }
    }

    fn fd_is_open(fd: i32) -> bool {
        // SAFETY: F_GETFD on an arbitrary integer is safe; it reports EBADF
        // for descriptors that are not open.
        unsafe { libc::fcntl(fd, libc::F_GETFD) != -1 }
    }

    #[test]
    fn borrowed_reader_stays_open_after_panicking_operation() {
        let (read_end, _write_end) = make_pipe();
        let raw_fd = read_end.as_raw_fd();

        let outcome = catch_unwind(AssertUnwindSafe(|| {
            with_borrowed_reader(raw_fd, |_reader| -> () {
                panic!("simulated failure inside the borrowed scope");
            });
        }));

        assert!(outcome.is_err(), "the panic must propagate to the caller");
        assert!(
            fd_is_open(raw_fd),
            "a panicking operation must not close the caller-owned FD",
        );
        // `read_end` now drops and closes the FD exactly once, proving the
        // guard did not already close it (a double close would surface as
        // EBADF under stricter runtimes).
    }

    #[test]
    fn borrowed_reader_stays_open_and_usable_after_success() {
        let (read_end, write_end) = make_pipe();
        let raw_fd = read_end.as_raw_fd();

        {
            // SAFETY: duplicating an owned descriptor for a scoped writer.
            let mut writer =
                unsafe { std::fs::File::from_raw_fd(libc::dup(write_end.as_raw_fd())) };
            match writer.write_all(b"ping") {
                Ok(()) => {}
                Err(err) => panic!("pipe write failed: {err}"),
            }
        }
        drop(write_end);

        let collected = with_borrowed_reader(raw_fd, |reader| {
            // SAFETY: reading through the borrowed handle's raw descriptor.
            let mut file = unsafe {
                std::mem::ManuallyDrop::new(std::fs::File::from_raw_fd(reader.as_raw_fd()))
            };
            let mut data = Vec::new();
            match file.read_to_end(&mut data) {
                Ok(_) => {}
                Err(err) => panic!("pipe read failed: {err}"),
            }
            data
        });

        assert_eq!(collected, b"ping");
        assert!(fd_is_open(raw_fd), "the borrowed FD must remain open");
    }
}
