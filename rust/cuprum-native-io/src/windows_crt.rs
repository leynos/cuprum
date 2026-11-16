//! Owned Windows CRT descriptors and synchronous `_read` operations.
//!
//! The crate's audited rule is that safe operations accept resources the
//! process already owns, and that raw integers only become resources here.
//! A caller's CRT descriptor is therefore never borrowed directly: it is
//! duplicated first, so the number read from is one this module owns and
//! nothing else can close or reuse. See [`duplicate_crt_reader`] and
//! [`OwnedCrtFileDescriptor`].

use std::{io, marker::PhantomData};

use windows_sys::Win32::Foundation::{ERROR_INVALID_HANDLE, GetLastError, SetLastError};

/// Largest `buffer_size` the CRT accepts before it rejects the argument.
///
/// `_read` treats `buffer_size > INT_MAX` as an invalid parameter, so the bound
/// is the signed `int` maximum even though the parameter is `unsigned`.
const CRT_MAX_BUFFER_SIZE: u32 = i32::MAX as u32;

/// A borrowed Windows CRT file descriptor whose owner remains external.
///
/// Construction is private to this module: a borrow can only be obtained from
/// an [`OwnedCrtFileDescriptor`], whose lifetime bounds it. That is what makes
/// the read safe without an `unsafe` constructor for callers to get wrong —
/// the ownership the borrow depends on is already established.
///
/// ```compile_fail,E0451
/// use cuprum_native_io::BorrowedCrtFileDescriptor;
///
/// // The fields are private, so no caller can name a descriptor number that
/// // nothing owns. The control below shows the supported route.
/// let _forged = BorrowedCrtFileDescriptor {
///     raw: 0,
///     _owner: std::marker::PhantomData,
/// };
/// ```
///
/// The supported route takes ownership of a duplicate first. This control
/// pins the public shape, and it names the same item the rejection case does
/// so a renamed export fails the control rather than letting that case pass
/// for the wrong reason:
///
/// ```
/// use cuprum_native_io::{
///     BorrowedCrtFileDescriptor,
///     OwnedCrtFileDescriptor,
///     duplicate_crt_reader,
/// };
///
/// let _take: fn(i32) -> std::io::Result<OwnedCrtFileDescriptor> = duplicate_crt_reader;
/// let _lend: for<'a> fn(&'a OwnedCrtFileDescriptor) -> BorrowedCrtFileDescriptor<'a> =
///     OwnedCrtFileDescriptor::as_borrowed;
/// ```
#[derive(Clone, Copy, Debug)]
pub struct BorrowedCrtFileDescriptor<'owner> {
    raw: i32,
    _owner: PhantomData<&'owner i32>,
}

/// An owned duplicate of a caller-supplied Windows CRT descriptor.
///
/// The value names a descriptor number no other part of the process holds, so
/// it cannot be closed or reused underneath a read. Dropping it closes the
/// duplicate; the caller's own descriptor is left open and keeps its
/// translation mode.
#[derive(Debug)]
pub struct OwnedCrtFileDescriptor {
    raw: i32,
}

impl OwnedCrtFileDescriptor {
    /// Borrow the owned duplicate for one synchronous CRT `_read` call.
    ///
    /// The returned borrow cannot outlive `self`, which is exactly the
    /// precondition [`BorrowedCrtFileDescriptor`] documents: while the borrow
    /// lives, the descriptor stays open and keeps referring to the same
    /// resource, because this module is its only owner.
    #[must_use]
    pub const fn as_borrowed(&self) -> BorrowedCrtFileDescriptor<'_> {
        BorrowedCrtFileDescriptor {
            raw: self.raw,
            _owner: PhantomData,
        }
    }
}

impl Drop for OwnedCrtFileDescriptor {
    fn drop(&mut self) {
        // SAFETY: this value is the duplicate's only owner and every borrow of
        // it has expired by the time it drops, so the descriptor is closed
        // exactly once and never afterwards reused.
        let _ = unsafe { libc::close(self.raw) };
    }
}

/// Duplicate a caller-supplied CRT descriptor into binary mode.
///
/// Taking ownership of a duplicate is what makes the result safe to read from
/// without the caller's cooperation. The CRT's `_dup` hands back a descriptor
/// number that no other part of the process holds, so nothing can close or
/// reuse it while a read is in flight; the caller may close its own
/// descriptor, or hand the same number to another call, without affecting the
/// duplicate. `_dup` shares the file position, which is what a consume
/// operation wants, and the duplicate's translation mode is set to binary here
/// so the caller's own mode is neither relied on nor changed.
///
/// Only ownership and translation mode are established. Whether the source
/// descriptor wraps a *synchronous* handle stays the caller's obligation:
/// Win32 offers no documented query for that property, and the CRT reports an
/// overlapped handle as a read error rather than corrupting memory.
///
/// The source descriptor must be open. A closed or otherwise invalid
/// descriptor is reported through the same classifier a failed read uses, so
/// the CRT's `EBADF` arrives as Win32's invalid-handle code instead of as a
/// bare `errno` the Python boundary would read as an unrelated Win32 number.
///
/// # Errors
/// Returns the Win32 error recorded by the CRT when `_dup` or `_setmode`
/// rejects the descriptor, including the closed-descriptor case.
pub fn duplicate_crt_reader(raw: i32) -> io::Result<OwnedCrtFileDescriptor> {
    let _handler = CrtInvalidParameterHandler::install();
    clear_crt_error_state();
    // SAFETY: `_dup` only reads the CRT descriptor table and returns either a
    // new descriptor it owns or -1 with the CRT error slots set; it takes no
    // pointer and no ownership from the caller.
    let duplicate = unsafe { libc::dup(raw) };
    if duplicate < 0 {
        return Err(crt_read_error());
    }
    // SAFETY: `duplicate` is a CRT descriptor this call now owns, and
    // `_setmode` only rewrites that descriptor's translation flag.
    if unsafe { setmode(duplicate, libc::O_BINARY) } < 0 {
        let error = crt_read_error();
        // SAFETY: the duplicate is still uniquely owned here, so closing it
        // before returning cannot race another owner.
        let _ = unsafe { libc::close(duplicate) };
        return Err(error);
    }
    Ok(OwnedCrtFileDescriptor { raw: duplicate })
}

/// Read once from a borrowed Windows CRT descriptor.
///
/// `_read` is used instead of `ReadFile`: the descriptor is a CRT number, not
/// a Win32 handle, and only the CRT knows how to read from one.
///
/// The CRT treats a malformed `_read` argument as an invalid parameter. Its
/// default handler terminates the process rather than returning an error, so a
/// descriptor that is open but not readable would abort the interpreter
/// instead of surfacing as an `io::Error`. A thread-local handler is installed
/// for the duration of the call so `_read` takes its documented continuation
/// path and reports the failure through `errno`. Ownership and binary mode are
/// already established by [`OwnedCrtFileDescriptor`]; only the synchronous
/// suitability of the underlying handle stays the caller's obligation.
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

    // SAFETY: the borrow came from an owner that keeps this CRT entry open for
    // the call, and the exclusive slice is writable for `count`.
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

/// Collect the CRT's error slots into the native code the caller sees.
fn crt_read_error() -> io::Error {
    let mut dos_error = 0_i32;
    let mut crt_errno = 0_i32;
    // SAFETY: GetLastError only observes this thread's Win32 error slot, so it
    // is captured before asking the CRT for its independent error fields.
    let win32_error = unsafe { GetLastError() };
    // SAFETY: both CRT functions write one initialized scalar output. The
    // result is read immediately on the same thread as the failed call.
    unsafe {
        get_doserrno(&mut dos_error);
        get_errno(&mut crt_errno);
    }

    classify_crt_error(dos_error, win32_error, crt_errno)
}

/// Map the CRT's three error channels onto the native code the caller sees.
///
/// A CRT call reports failure through whichever channel matches its path: a
/// DOS error for unterminated text-mode input, `GetLastError` for a failed
/// Win32 call, and `errno` alone for the descriptor rejections the CRT detects
/// itself. The channels are consulted in source order so the first populated
/// one wins.
///
/// Only `EBADF` is translated, and only because the CRT's invalid-parameter
/// continuation path reports a closed descriptor that way. The CRT `errno`
/// space is *not* the Win32 error space, so the remaining values cannot be
/// passed through as if they were native codes: `EIO` is 5, which would be
/// read back as `ERROR_ACCESS_DENIED`, and `EACCES` is 13, which names nothing
/// in the Win32 table. An unrecognized `errno` therefore stays opaque rather
/// than acquiring a meaning it does not have.
///
/// This is separate from [`crt_read_error`] so it can be exercised directly.
/// `_read` validates its descriptor before touching the buffer, so a test
/// cannot reach its branches by passing a bad descriptor; [`duplicate_crt_reader`]
/// reaches them through `_dup`, which reports a closed descriptor as `EBADF`.
fn classify_crt_error(dos_error: i32, win32_error: u32, crt_errno: i32) -> io::Error {
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
    #[link_name = "_setmode"]
    fn setmode(fd: i32, mode: i32) -> i32;
    #[link_name = "_set_thread_local_invalid_parameter_handler"]
    fn set_thread_local_invalid_parameter_handler(
        handler: InvalidParameterHandler,
    ) -> InvalidParameterHandler;
}

#[cfg(test)]
#[path = "windows_crt_tests.rs"]
mod tests;
