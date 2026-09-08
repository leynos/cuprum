//! Audited resource ownership and single-call native I/O for Cuprum.
//!
//! Safe operations accept lifetime-bound OS borrows. Only the Python
//! integration boundary may reconstruct resources from raw integers.
#![deny(unsafe_op_in_unsafe_fn)]

#[cfg(unix)]
use std::io;
#[cfg(kani)]
mod kani_proofs;
mod memory;
pub mod progress;
pub use memory::with_owned_writer;
#[cfg(kani)]
mod fd_ownership_kani_proofs;
#[cfg(any(test, kani))]
mod fd_ownership_model;

#[cfg(unix)]
pub use std::os::fd::{AsFd as AsStream, BorrowedFd as BorrowedStream, OwnedFd as OwnedStream};
#[cfg(windows)]
pub use std::os::windows::io::{
    AsHandle as AsStream, BorrowedHandle as BorrowedStream, OwnedHandle as OwnedStream,
};

#[cfg(unix)]
use std::os::fd::{AsRawFd, FromRawFd};
#[cfg(windows)]
use std::os::windows::io::{FromRawHandle, RawHandle};

/// Integer representation at the Python ABI boundary, not an ownership token.
#[cfg(unix)]
pub type PlatformFd = i32;
/// Pointer-width integer representation at the Python ABI boundary.
#[cfg(windows)]
pub type PlatformFd = usize;

/// Borrow a resource without transferring or extending its ownership.
pub fn borrow(stream: &impl AsStream) -> BorrowedStream<'_> {
    #[cfg(unix)]
    {
        stream.as_fd()
    }
    #[cfg(windows)]
    {
        stream.as_handle()
    }
}

/// Accept the uniquely owned writer transferred by the integration boundary.
///
/// # Safety
/// `raw` must denote a valid open resource owned exclusively by the caller.
/// The caller relinquishes ownership and must never close or reuse it after
/// this call. Range validation alone does not establish these obligations.
#[must_use]
pub unsafe fn adopt_writer(raw: PlatformFd) -> OwnedStream {
    #[cfg(unix)]
    {
        // SAFETY: the caller transfers a valid, uniquely owned descriptor.
        unsafe { OwnedStream::from_raw_fd(raw) }
    }
    #[cfg(windows)]
    {
        // SAFETY: the caller transfers a valid independently duplicated Win32
        // handle, not a CRT descriptor. The cast preserves pointer width.
        unsafe { OwnedStream::from_raw_handle(raw as RawHandle) }
    }
}

/// Borrow a raw reader whose owner is maintained by the integration layer.
///
/// # Safety
/// `raw` must stay open and refer to the same resource throughout `'owner`.
/// The caller must prevent close/reuse, including during GIL release and
/// cancellation. It retains responsibility for closing the resource.
#[must_use]
pub const unsafe fn borrow_reader<'owner>(raw: PlatformFd) -> BorrowedStream<'owner> {
    #[cfg(unix)]
    {
        // SAFETY: the caller guarantees validity for the returned lifetime.
        unsafe { BorrowedStream::borrow_raw(raw) }
    }
    #[cfg(windows)]
    {
        // SAFETY: the caller guarantees validity for the returned lifetime;
        // the integer is a pointer-width Win32 handle, not a CRT descriptor.
        unsafe { BorrowedStream::borrow_raw(raw as RawHandle) }
    }
}

/// Read once into initialized storage, retaining the resource borrow.
///
/// # Errors
/// Returns the native I/O error, including interruption, without retrying.
#[cfg(unix)]
pub fn read_once(stream: BorrowedStream<'_>, buffer: &mut [u8]) -> io::Result<isize> {
    // SAFETY: the descriptor borrow remains live; the exclusive slice is
    // writable for its length. read does not retain the buffer pointer.
    let result =
        unsafe { libc::read(stream.as_raw_fd(), buffer.as_mut_ptr().cast(), buffer.len()) };
    syscall_result(result, buffer.len())
}

/// Write once from initialized storage, retaining the resource borrow.
///
/// # Errors
/// Returns the native I/O error, including interruption, without retrying.
#[cfg(unix)]
pub fn write_once(stream: BorrowedStream<'_>, buffer: &[u8]) -> io::Result<isize> {
    // SAFETY: the descriptor borrow remains live; the shared slice is
    // readable for its length. write does not retain or mutate the buffer.
    let result = unsafe { libc::write(stream.as_raw_fd(), buffer.as_ptr().cast(), buffer.len()) };
    syscall_result(result, buffer.len())
}

#[cfg(unix)]
fn syscall_result(result: isize, capacity: usize) -> io::Result<isize> {
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        let count = usize::try_from(result).map_err(io::Error::other)?;
        progress::checked_count(count, capacity).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "native count exceeded buffer")
        })?;
        Ok(result)
    }
}

/// Transfer once between borrowed Linux descriptors using kernel splice.
///
/// # Errors
/// Returns the syscall error unchanged; policy decides retry and fallback.
#[cfg(target_os = "linux")]
pub fn splice_once(
    reader: BorrowedStream<'_>,
    writer: BorrowedStream<'_>,
    length: usize,
) -> io::Result<isize> {
    // SAFETY: both descriptor borrows remain live. Null offsets request the
    // file offsets (and are required for pipes); no userspace buffer escapes.
    let result = unsafe {
        libc::splice(
            reader.as_raw_fd(),
            std::ptr::null_mut(),
            writer.as_raw_fd(),
            std::ptr::null_mut(),
            length,
            libc::SPLICE_F_MOVE | libc::SPLICE_F_MORE,
        )
    };
    syscall_result(result, length)
}

/// Create two uniquely owned pipe endpoints.
///
/// # Errors
/// Returns the pipe creation error without constructing owners on failure.
#[cfg(unix)]
pub fn pipe() -> io::Result<(OwnedStream, OwnedStream)> {
    let mut endpoints = [0; 2];
    // SAFETY: endpoints provides writable storage for exactly two integers.
    let result = unsafe { libc::pipe(endpoints.as_mut_ptr()) };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    let [reader, writer] = endpoints;
    // SAFETY: successful pipe returns two distinct, newly owned descriptors.
    Ok(unsafe {
        (
            OwnedStream::from_raw_fd(reader),
            OwnedStream::from_raw_fd(writer),
        )
    })
}

/// Observe whether a descriptor is currently open, for native regressions.
///
/// This observation grants no ownership or lifetime guarantee and must never
/// be used to justify subsequent raw-resource reconstruction.
#[cfg(unix)]
#[must_use]
pub fn fd_is_open(fd: i32) -> bool {
    loop {
        // SAFETY: F_GETFD accepts arbitrary descriptor integers, has no pointer
        // argument, and reports EBADF without touching memory for closed FDs.
        let result = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if result != -1 {
            return true;
        }
        if io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
            return false;
        }
    }
}

#[cfg(test)]
mod ownership_tests;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::{fd_is_open, pipe, read_once, write_once};
