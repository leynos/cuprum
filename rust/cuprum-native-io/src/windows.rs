//! Windows pipe creation and handle-observation primitives for native tests.

use std::io;
use std::os::windows::io::FromRawHandle;

use windows_sys::Win32::Foundation::GetHandleInformation;
use windows_sys::Win32::System::Pipes::CreatePipe;

use crate::{OwnedStream, PlatformFd};

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
