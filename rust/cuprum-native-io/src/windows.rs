//! Windows borrowed I/O, pipe ownership, and native handle observation.

use std::io::{self, Read, Write};
use std::os::windows::io::{AsRawHandle, FromRawHandle};

use windows_sys::Win32::Foundation::GetHandleInformation;
use windows_sys::Win32::System::Pipes::CreatePipe;

use crate::{BorrowedStream, OwnedStream, PlatformFd};

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

/// Observe whether a handle is currently open, without granting ownership.
///
/// The result must not be used as a check-then-use validity guarantee.
#[must_use]
pub fn fd_is_open(raw: PlatformFd) -> bool {
    let mut flags = 0;
    // SAFETY: GetHandleInformation validates the opaque handle and writes
    // flags only through the live output pointer. No ownership is acquired.
    unsafe { GetHandleInformation(raw as _, &mut flags) != 0 }
}

/// Read once into initialized storage, retaining the handle borrow.
///
/// # Errors
/// Returns the native I/O error, including interruption, without retrying.
pub fn read_once(stream: BorrowedStream<'_>, buffer: &mut [u8]) -> io::Result<isize> {
    with_file(stream, |file| file.read(buffer))
}

/// Write once from initialized storage, retaining the handle borrow.
///
/// # Errors
/// Returns the native I/O error, including interruption, without retrying.
pub fn write_once(stream: BorrowedStream<'_>, buffer: &[u8]) -> io::Result<isize> {
    with_file(stream, |file| file.write(buffer))
}

pub(super) fn with_file(
    stream: BorrowedStream<'_>,
    operation: impl FnOnce(&mut cap_std::fs::File) -> io::Result<usize>,
) -> io::Result<isize> {
    // SAFETY: the borrowed handle is valid during this scope. ManuallyDrop
    // prevents closing it on normal return and unwind. This private helper
    // only invokes read/write; callers cannot replace or move out the File.
    let file = unsafe { cap_std::fs::File::from_raw_handle(stream.as_raw_handle()) };
    crate::memory::with_retained_owner(file, operation)
        .and_then(|count| isize::try_from(count).map_err(io::Error::other))
}
