//! Borrowed CRT descriptors and synchronous `_read` operations for consume.

use std::{io, marker::PhantomData};

use windows_sys::Win32::Foundation::{ERROR_INVALID_HANDLE, GetLastError, SetLastError};

/// A borrowed Windows CRT file descriptor whose owner remains external.
#[derive(Clone, Copy, Debug)]
pub struct BorrowedCrtFileDescriptor<'owner> {
    raw: i32,
    _owner: PhantomData<&'owner i32>,
}

/// Borrow a CRT descriptor for one synchronous native consume operation.
///
/// # Safety
/// `raw` must be a valid open CRT descriptor and must continue to refer to the
/// same resource for `'owner`. The owner must prevent closing or reusing the
/// descriptor until the operation completes. The descriptor must be suitable
/// for synchronous CRT `_read` calls.
#[must_use]
pub const unsafe fn borrow_crt_reader<'owner>(raw: i32) -> BorrowedCrtFileDescriptor<'owner> {
    BorrowedCrtFileDescriptor {
        raw,
        _owner: PhantomData,
    }
}

/// Read once from a borrowed Windows CRT descriptor.
///
/// `_read` is used instead of `ReadFile`: the CRT descriptor is the resource
/// Python duplicates and places in binary mode before calling the extension.
///
/// # Errors
/// Returns the Win32 error recorded by the CRT, preserving the native code.
pub fn read_crt_once(
    stream: BorrowedCrtFileDescriptor<'_>,
    buffer: &mut [u8],
) -> io::Result<isize> {
    let count = u32::try_from(buffer.len()).map_err(io::Error::other)?;
    clear_crt_error_state();

    // SAFETY: the caller's borrowed-descriptor contract keeps this CRT entry
    // open for the call, and the exclusive slice is writable for `count`.
    let result = unsafe { libc::read(stream.raw, buffer.as_mut_ptr().cast(), count) };
    if result < 0 {
        Err(crt_read_error())
    } else {
        let read_count = usize::try_from(result).map_err(io::Error::other)?;
        if read_count > buffer.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "CRT read exceeded its buffer",
            ));
        }
        isize::try_from(read_count).map_err(io::Error::other)
    }
}

fn clear_crt_error_state() {
    // SAFETY: the CRT setters take scalar values and mutate only this thread's
    // error slots. Clearing them prevents an unrelated earlier failure from
    // being reported as the result of `_read`.
    unsafe {
        set_errno(0);
        set_doserrno(0);
    }
    // SAFETY: SetLastError writes this thread's Win32 error slot.
    unsafe { SetLastError(0) };
}

fn crt_read_error() -> io::Error {
    let mut dos_error = 0_i32;
    let mut crt_errno = 0_i32;
    // SAFETY: GetLastError only observes this thread's Win32 error slot, so it
    // is captured before asking the CRT for its independent error fields.
    let win32_error = unsafe { GetLastError() };
    // SAFETY: both CRT functions write one initialized scalar output. The
    // result is read immediately on the same thread as the failed `_read`.
    unsafe {
        get_doserrno(&mut dos_error);
        get_errno(&mut crt_errno);
    }

    let native_error = if dos_error != 0 {
        Some(dos_error)
    } else if win32_error != 0 {
        i32::try_from(win32_error).ok()
    } else if crt_errno == libc::EBADF {
        i32::try_from(ERROR_INVALID_HANDLE).ok()
    } else {
        None
    };
    native_error.map_or_else(
        || io::Error::other(format!("CRT read failed with errno {crt_errno}")),
        io::Error::from_raw_os_error,
    )
}

unsafe extern "C" {
    #[link_name = "_get_doserrno"]
    fn get_doserrno(value: *mut i32) -> i32;
    #[link_name = "_get_errno"]
    fn get_errno(value: *mut i32) -> i32;
    #[link_name = "_set_doserrno"]
    fn set_doserrno(value: i32) -> i32;
    #[link_name = "_set_errno"]
    fn set_errno(value: i32) -> i32;
}

#[cfg(test)]
mod tests {
    //! Exercise the Windows CRT reader with real CRT pipe descriptors.

    use std::{io, mem::MaybeUninit};

    use rstest::rstest;
    use windows_sys::Win32::Foundation::ERROR_INVALID_HANDLE;

    use super::{borrow_crt_reader, read_crt_once};

    struct OwnedCrtDescriptor(i32);

    impl Drop for OwnedCrtDescriptor {
        fn drop(&mut self) {
            // SAFETY: this test wrapper uniquely owns the CRT descriptor.
            let _ = unsafe { libc::close(self.0) };
        }
    }

    fn crt_pipe() -> io::Result<(OwnedCrtDescriptor, OwnedCrtDescriptor)> {
        let mut descriptors = [MaybeUninit::<i32>::uninit(); 2];
        // SAFETY: `_pipe` initializes both descriptor outputs on success and
        // the array has exactly the required two scalar slots.
        let result = unsafe { libc::pipe(descriptors.as_mut_ptr().cast(), 4096, libc::O_BINARY) };
        if result != 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: successful `_pipe` initialized both output descriptors.
        let reader = unsafe { descriptors[0].assume_init() };
        // SAFETY: successful `_pipe` initialized both output descriptors.
        let writer = unsafe { descriptors[1].assume_init() };
        Ok((OwnedCrtDescriptor(reader), OwnedCrtDescriptor(writer)))
    }

    #[rstest]
    fn reads_binary_pipe_bytes_to_eof() {
        let (reader, writer) = crt_pipe().expect("CRT pipe should be created");
        let payload = b"line\r\n\x1aend";
        // SAFETY: the test owns `writer`; payload remains readable through its
        // complete length for the duration of `_write`.
        let written = unsafe {
            libc::write(
                writer.0,
                payload.as_ptr().cast(),
                u32::try_from(payload.len()).expect("short test payload fits u32"),
            )
        };
        assert_eq!(written, i32::try_from(payload.len()).expect("fits i32"));
        drop(writer);

        // SAFETY: `reader` remains uniquely owned and open until this test ends.
        let borrowed = unsafe { borrow_crt_reader(reader.0) };
        let mut buffer = [0; 32];
        let count = read_crt_once(borrowed, &mut buffer).expect("binary CRT read should succeed");
        assert_eq!(count, isize::try_from(payload.len()).expect("fits isize"));
        assert_eq!(buffer.get(..payload.len()), Some(payload.as_slice()));
        assert_eq!(
            read_crt_once(borrowed, &mut buffer).expect("EOF read should succeed"),
            0,
        );
    }

    #[rstest]
    fn maps_invalid_crt_descriptor_to_windows_error() {
        // SAFETY: this deliberate invalid value tests that `_read` fails before
        // dereferencing any buffer pointer or acquiring ownership.
        let borrowed = unsafe { borrow_crt_reader(i32::MAX) };
        let error =
            read_crt_once(borrowed, &mut [0; 1]).expect_err("invalid CRT descriptor must fail");
        assert_eq!(
            error.raw_os_error(),
            i32::try_from(ERROR_INVALID_HANDLE).ok(),
        );
    }
}
