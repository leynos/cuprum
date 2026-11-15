//! Borrowed CRT descriptors and synchronous `_read` operations for consume.

use std::{io, marker::PhantomData};

use windows_sys::Win32::Foundation::{ERROR_INVALID_HANDLE, GetLastError, SetLastError};

/// Largest `buffer_size` the CRT accepts before it rejects the argument.
///
/// `_read` treats `buffer_size > INT_MAX` as an invalid parameter, so the bound
/// is the signed `int` maximum even though the parameter is `unsigned`.
const CRT_MAX_BUFFER_SIZE: u32 = i32::MAX as u32;

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
/// The CRT treats a malformed `_read` argument as an invalid parameter. Its
/// default handler terminates the process rather than returning an error, so a
/// descriptor that is closed, unreadable, or locked would abort the interpreter
/// instead of surfacing as an `io::Error`. A thread-local handler is installed
/// for the duration of the call so `_read` takes its documented continuation
/// path and reports the failure through `errno`. The descriptor's access mode
/// and synchronous suitability stay the caller's obligation.
///
/// # Errors
/// Returns the Win32 error recorded by the CRT, preserving the native code, and
/// rejects a buffer larger than the CRT's signed `buffer_size` limit before the
/// call because the CRT would treat that as an invalid parameter too.
pub fn read_crt_once(
    stream: BorrowedCrtFileDescriptor<'_>,
    buffer: &mut [u8],
) -> io::Result<isize> {
    let count = crt_read_count(buffer.len())?;
    let _handler = CrtInvalidParameterHandler::install();
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

/// Convert a buffer length into the CRT's `unsigned buffer_size` argument.
///
/// `_read` invokes the invalid parameter handler when `buffer_size` exceeds
/// `INT_MAX`, so the limit is enforced here rather than left to the CRT, whose
/// default handler would terminate the process.
fn crt_read_count(length: usize) -> io::Result<u32> {
    let count = u32::try_from(length).map_err(io::Error::other)?;
    if count > CRT_MAX_BUFFER_SIZE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "CRT read buffer exceeds the signed buffer_size limit",
        ));
    }
    Ok(count)
}

/// Holds a thread-local CRT invalid parameter handler for one `_read` call.
///
/// The previous handler is restored on drop, including while unwinding, so the
/// process-wide default is never left replaced for another thread's work.
struct CrtInvalidParameterHandler {
    previous: InvalidParameterHandler,
}

impl CrtInvalidParameterHandler {
    /// Install the continuing handler on this thread for the current scope.
    fn install() -> Self {
        // SAFETY: the handler has the CRT's documented `_invalid_parameter`
        // prototype, `_set_thread_local_invalid_parameter_handler` returns the
        // handler it replaced, and the value is only forwarded to the matching
        // setter when this guard drops on the same thread.
        unsafe {
            let previous =
                set_thread_local_invalid_parameter_handler(Some(ignore_invalid_parameter));
            Self { previous }
        }
    }
}

impl Drop for CrtInvalidParameterHandler {
    fn drop(&mut self) {
        // SAFETY: restores the handler this guard replaced. Both values are
        // handles the CRT returned, restored on the thread that installed them.
        unsafe {
            set_thread_local_invalid_parameter_handler(self.previous);
        }
    }
}

/// Permit `_read` to continue after an invalid parameter and return its error.
///
/// This deliberately does nothing. The handler exists only so the CRT takes
/// its documented `-1` return path, which `read_crt_once` then maps to an
/// `io::Error`, instead of calling the default handler that terminates the
/// process.
const unsafe extern "C" fn ignore_invalid_parameter(
    _expression: *const u16,
    _function: *const u16,
    _file: *const u16,
    _line: u32,
    _reserved: usize,
) {
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

    classify_crt_read_error(dos_error, win32_error, crt_errno)
}

/// Map the CRT's three error channels onto the native code the caller sees.
///
/// `_read` reports failure through whichever channel matches its path: a DOS
/// error for unterminated text-mode input, `GetLastError` for a failed Win32
/// call, and `errno` alone for the descriptor failures the CRT detects itself.
/// The fixups are applied in source order so the first populated channel wins.
///
/// This is separate from [`crt_read_error`] so it can be exercised directly.
/// `_read` validates its descriptor before touching the buffer, and Microsoft
/// documents that the invalid parameter handler then terminates the process, so
/// a test cannot reach these branches by passing a bad descriptor.
fn classify_crt_read_error(dos_error: i32, win32_error: u32, crt_errno: i32) -> io::Error {
    let native_error = if dos_error != 0 {
        Some(dos_error)
    } else if win32_error != 0 {
        i32::try_from(win32_error).ok()
    } else if crt_errno == libc::EBADF {
        i32::try_from(ERROR_INVALID_HANDLE).ok()
    } else {
        // `errno` is already a native code for the two descriptor rejections
        // `_read` performs itself: `EBADF` for a closed descriptor, which needs
        // the mapping above, and `EACCES` for an open but unreadable one, which
        // is returned as reported. Leaving `EACCES` unmapped cannot hide a
        // defect: `ERROR_INVALID_HANDLE` is 6 while `EACCES` is 13.
        crt_errno.checked_abs().filter(|code| *code != 0)
    };
    native_error.map_or_else(
        || io::Error::other(format!("CRT read failed with errno {crt_errno}")),
        io::Error::from_raw_os_error,
    )
}

/// The CRT's `_invalid_parameter` function pointer type.
///
/// The CRT passes `wchar_t` arguments as UTF-16 pointers; they are `null` for a
/// release-mode CRT, which is why the handler ignores them.
type InvalidParameterHandler =
    Option<unsafe extern "C" fn(*const u16, *const u16, *const u16, u32, usize)>;

unsafe extern "C" {
    #[link_name = "_get_doserrno"]
    fn get_doserrno(value: *mut i32) -> i32;
    #[link_name = "_get_errno"]
    fn get_errno(value: *mut i32) -> i32;
    #[link_name = "_set_doserrno"]
    fn set_doserrno(value: i32) -> i32;
    #[link_name = "_set_errno"]
    fn set_errno(value: i32) -> i32;
    #[link_name = "_set_thread_local_invalid_parameter_handler"]
    fn set_thread_local_invalid_parameter_handler(
        handler: InvalidParameterHandler,
    ) -> InvalidParameterHandler;
}

#[cfg(test)]
mod tests {
    //! Exercise the Windows CRT reader with real CRT pipe descriptors.

    use std::{io, mem::MaybeUninit};

    use rstest::rstest;
    use windows_sys::Win32::Foundation::ERROR_INVALID_HANDLE;

    use super::{
        CRT_MAX_BUFFER_SIZE,
        borrow_crt_reader,
        classify_crt_read_error,
        crt_read_count,
        read_crt_once,
    };

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
    #[case::ebadf_becomes_invalid_handle(0, 0, libc::EBADF, ERROR_INVALID_HANDLE)]
    // `_read` on an open but unreadable descriptor sets `EACCES` and leaves the
    // other two channels clear, so this is the shape a write-only descriptor
    // produces. It is separate from the `EBADF` mapping above.
    #[case::eacces_is_reported_as_reported(0, 0, libc::EACCES, 13)]
    #[case::dos_error_wins_over_win32(5, 6, libc::EBADF, 5)]
    #[case::win32_error_wins_over_crt_errno(0, 6, libc::EBADF, 6)]
    fn classifies_crt_read_failure(
        #[case] dos_error: i32,
        #[case] win32_error: u32,
        #[case] crt_errno: i32,
        #[case] expected_native: u32,
    ) {
        let error = classify_crt_read_error(dos_error, win32_error, crt_errno);
        assert_eq!(
            error.raw_os_error(),
            i32::try_from(expected_native).ok(),
            "unexpected mapping for ({dos_error}, {win32_error}, {crt_errno})",
        );
    }

    #[rstest]
    #[case::unclassified_errno(0, 0, libc::EIO)]
    #[case::no_channel_populated(0, 0, 0)]
    fn reports_unclassified_crt_failure_as_an_opaque_error(
        #[case] dos_error: i32,
        #[case] win32_error: u32,
        #[case] crt_errno: i32,
    ) {
        let error = classify_crt_read_error(dos_error, win32_error, crt_errno);
        assert_eq!(
            error.raw_os_error(),
            None,
            "an unclassified CRT failure must stay opaque for ({dos_error}, {win32_error}, \
             {crt_errno})",
        );
    }

    #[test]
    fn rejects_a_buffer_above_the_crt_signed_limit() {
        let length = usize::try_from(CRT_MAX_BUFFER_SIZE).expect("u32 fits usize") + 1;
        let error = crt_read_count(length).expect_err("the CRT limit must reject the length");
        assert_eq!(
            error.kind(),
            io::ErrorKind::InvalidInput,
            "an oversized buffer must be reported as invalid input, got {error}",
        );
        assert_eq!(
            crt_read_count(usize::try_from(CRT_MAX_BUFFER_SIZE).expect("u32 fits usize"))
                .expect("the exact limit is accepted"),
            CRT_MAX_BUFFER_SIZE,
            "the CRT's documented maximum must remain readable",
        );
    }

    #[test]
    fn reports_a_closed_descriptor_instead_of_terminating_the_process() {
        let (reader, writer) = crt_pipe().expect("CRT pipe should be created");
        let raw = reader.0;
        // Close the descriptor while `reader` still owns the raw value, then
        // leak the owner so it cannot close the number a second time. The raw
        // number stays a valid `i32`, which is what `borrow_crt_reader` takes.
        // SAFETY: this test owns `reader` and closes its descriptor once here.
        let closed = unsafe { libc::close(raw) };
        assert_eq!(closed, 0, "the CRT descriptor must close for this test");
        std::mem::forget(reader);
        drop(writer);

        // SAFETY: `raw` is a closed descriptor, which is exactly the contract
        // violation under test. Reading it is how the process-crash path would
        // have been reached; the installed handler turns that into an error.
        let borrowed = unsafe { borrow_crt_reader(raw) };
        let mut buffer = [0; 8];
        let error = read_crt_once(borrowed, &mut buffer)
            .expect_err("a closed descriptor must report an error, not abort");

        assert_eq!(
            error.raw_os_error(),
            i32::try_from(ERROR_INVALID_HANDLE).ok(),
            "a closed CRT descriptor must map to the invalid-handle code, got {error}",
        );
    }

    #[test]
    fn reports_a_write_only_descriptor_instead_of_terminating_the_process() {
        let (reader, writer) = crt_pipe().expect("CRT pipe should be created");
        drop(reader);
        // SAFETY: `writer` is a CRT descriptor this test uniquely owns and has
        // not closed. Borrowing it as a reader is the misuse under test: the
        // descriptor is open, so it passes any range check, but not readable.
        let borrowed = unsafe { borrow_crt_reader(writer.0) };
        let mut buffer = [0; 8];
        let error = read_crt_once(borrowed, &mut buffer)
            .expect_err("a write-only descriptor must report an error, not abort");

        assert_eq!(
            error.raw_os_error(),
            Some(libc::EACCES),
            "an unreadable CRT descriptor must report the access-denied code the CRT sets, not \
             the closed-descriptor mapping, got {error}",
        );
    }
}
