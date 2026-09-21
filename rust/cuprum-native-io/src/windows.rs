//! Windows borrowed I/O, pipe ownership, and native handle observation.

use std::{
    io::{self, Read, Write},
    os::windows::io::{AsRawHandle, FromRawHandle},
};

#[cfg(test)]
use windows_sys::Win32::Foundation::{ERROR_INVALID_HANDLE, GetHandleInformation};
use windows_sys::Win32::System::Pipes::CreatePipe;

#[cfg(test)]
use crate::PlatformFd;
use crate::{AsStream, BorrowedStream, OwnedStream};

/// A borrowed stream known to support synchronous Windows I/O.
///
/// `cap_std::fs::File` implements [`Read`] and [`Write`] through blocking
/// `ReadFile` and `WriteFile` semantics. The wrapped handle must therefore be
/// synchronous: it must not have been opened with `FILE_FLAG_OVERLAPPED`.
/// Win32 provides no documented query that can establish this property from a
/// bare handle, so construction is limited to known synchronous resources or
/// audited integration boundaries.
#[derive(Clone, Copy, Debug)]
pub struct SynchronousBorrowedStream<'a>(BorrowedStream<'a>);

impl<'a> SynchronousBorrowedStream<'a> {
    /// Construct a synchronous-I/O capability from an audited borrowed handle.
    ///
    /// # Safety
    /// `handle` must remain valid for `'a` and must support blocking
    /// synchronous `ReadFile` and `WriteFile` semantics. In particular, it
    /// must not be a handle opened with `FILE_FLAG_OVERLAPPED`; callers must
    /// establish this when the handle is created, duplicated, or handed off.
    #[must_use]
    pub const unsafe fn new_unchecked(handle: BorrowedStream<'a>) -> Self { Self(handle) }

    /// Return the capability's underlying handle inside the native boundary.
    pub(crate) const fn borrowed_handle(self) -> BorrowedStream<'a> { self.0 }

    const fn from_known_synchronous(handle: BorrowedStream<'a>) -> Self { Self(handle) }
}

impl AsStream for SynchronousBorrowedStream<'_> {
    fn as_handle(&self) -> BorrowedStream<'_> { self.0 }
}

/// An owned stream known to support synchronous Windows I/O.
///
/// This wrapper can safely lend [`SynchronousBorrowedStream`] because its
/// construction establishes the same non-overlapped I/O contract.
#[derive(Debug)]
pub struct SynchronousOwnedStream(OwnedStream);

impl SynchronousOwnedStream {
    /// Borrow this known synchronous resource for a single native operation.
    #[must_use]
    pub fn as_synchronous_borrowed(&self) -> SynchronousBorrowedStream<'_> {
        SynchronousBorrowedStream::from_known_synchronous(self.0.as_handle())
    }

    pub(crate) const fn from_known_synchronous(handle: OwnedStream) -> Self { Self(handle) }
}

impl AsStream for SynchronousOwnedStream {
    fn as_handle(&self) -> BorrowedStream<'_> { self.0.as_handle() }
}

/// Create two uniquely owned, non-inheritable anonymous pipe handles.
///
/// # Errors
/// Returns the Win32 error if no pipe could be created.
pub fn pipe() -> io::Result<(OwnedStream, OwnedStream)> {
    let mut reader = std::ptr::null_mut();
    let mut writer = std::ptr::null_mut();
    // SAFETY: both outputs are writable HANDLE slots. Null security attributes
    // select default security and non-inheritance; zero selects default size.
    let result = unsafe { CreatePipe(&mut reader, &mut writer, std::ptr::null(), 0) };
    if result == 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: success produced distinct valid pipe handles owned only here.
    Ok(unsafe {
        (
            OwnedStream::from_raw_handle(reader),
            OwnedStream::from_raw_handle(writer),
        )
    })
}

/// Create two owned anonymous-pipe handles for synchronous native I/O.
///
/// [`CreatePipe`] creates synchronous anonymous-pipe handles, so the returned
/// wrappers may safely lend [`SynchronousBorrowedStream`].
///
/// # Errors
/// Returns the Win32 error if no pipe could be created.
pub fn synchronous_pipe() -> io::Result<(SynchronousOwnedStream, SynchronousOwnedStream)> {
    pipe().map(|(reader, writer)| {
        (
            SynchronousOwnedStream::from_known_synchronous(reader),
            SynchronousOwnedStream::from_known_synchronous(writer),
        )
    })
}

/// Observe whether a handle is currently open, without granting ownership.
///
/// The result must not be used as a check-then-use validity guarantee. It is
/// test support, so it is neither exported nor compiled into the shipped
/// library.
///
/// # Errors
/// Returns the Win32 error for any failure other than the documented
/// invalid-handle condition. Recording those as "closed" would report an
/// ownership verdict the observation never established.
#[cfg(test)]
pub(crate) fn fd_is_open(raw: PlatformFd) -> io::Result<bool> {
    let mut flags = 0;
    // SAFETY: GetHandleInformation validates the opaque handle and writes
    // flags only through the live output pointer. No ownership is acquired.
    if unsafe { GetHandleInformation(raw as _, &mut flags) } != 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    // Win32 codes are unsigned; a negative `raw_os_error` cannot be one, so
    // the conversion to the constant's own type is the honest comparison.
    match error
        .raw_os_error()
        .and_then(|code| u32::try_from(code).ok())
    {
        Some(ERROR_INVALID_HANDLE) => Ok(false),
        _ => Err(error),
    }
}

/// Read once into initialized storage, retaining the handle borrow.
///
/// # Errors
/// Returns the native I/O error, including interruption, without retrying.
pub fn read_once(stream: SynchronousBorrowedStream<'_>, buffer: &mut [u8]) -> io::Result<isize> {
    with_file(stream, |file| file.read(buffer))
}

/// Write once from initialized storage, retaining the handle borrow.
///
/// # Errors
/// Returns the native I/O error, including interruption, without retrying.
pub fn write_once(stream: SynchronousBorrowedStream<'_>, buffer: &[u8]) -> io::Result<isize> {
    with_file(stream, |file| file.write(buffer))
}

pub(super) fn with_file(
    stream: SynchronousBorrowedStream<'_>,
    operation: impl FnOnce(&mut cap_std::fs::File) -> io::Result<usize>,
) -> io::Result<isize> {
    let handle = stream.borrowed_handle();
    // SAFETY: the capability establishes synchronous `ReadFile`/`WriteFile`
    // compatibility and the borrow remains valid during this scope.
    // ManuallyDrop prevents closing it on return and unwind. This private
    // helper invokes only read/write; callers cannot replace the File.
    let file = unsafe { cap_std::fs::File::from_raw_handle(handle.as_raw_handle()) };
    crate::memory::with_retained_owner(file, operation)
        .and_then(|count| isize::try_from(count).map_err(io::Error::other))
}
