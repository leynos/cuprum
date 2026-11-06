//! Windows borrowed I/O, pipe ownership, and native handle observation.

use std::{
    io::{self, Read, Write},
    os::windows::io::{AsRawHandle, FromRawHandle, IntoRawHandle, RawHandle},
};

#[cfg(test)]
use windows_sys::Win32::Foundation::{
    ERROR_INVALID_HANDLE,
    GetHandleInformation,
    HANDLE_FLAG_INHERIT,
};

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

impl IntoRawHandle for SynchronousOwnedStream {
    fn into_raw_handle(self) -> RawHandle { self.0.into_raw_handle() }
}

/// Create two owned anonymous-pipe handles for synchronous native I/O.
///
/// [`crate::pipe`] forwards to `std::io::pipe`, whose Rust 1.87.0 Windows half
/// calls `CreatePipe(&mut reader, &mut writer, ptr::null_mut(), 0)`. Two
/// documented Win32 facts make that result fit to lend
/// [`SynchronousBorrowedStream`]: `lpPipeAttributes` of `NULL` means the
/// returned handles cannot be inherited, and anonymous pipes do not support
/// asynchronous read or write operations at all — `ReadFile` and `WriteFile`
/// ignore their `lpOverlapped` argument for such a pipe. Every operation on
/// these ends is therefore synchronous and blocking.
///
/// The standard library documents only that the call currently corresponds to
/// `CreatePipe` and "may change in the future", so this is implementation
/// evidence from the pinned compiler rather than a public `std` guarantee. A
/// compiler bump must re-check the arguments at
/// `library/std/src/sys/anonymous_pipe/windows.rs` before the capability is
/// trusted again.
///
/// Foreign handles are unaffected: they still cannot enter the capability
/// except through [`SynchronousBorrowedStream::new_unchecked`] at an audited
/// hand-off.
///
/// # Errors
/// Returns the Win32 error if no pipe could be created.
pub fn synchronous_pipe() -> io::Result<(SynchronousOwnedStream, SynchronousOwnedStream)> {
    crate::pipe().map(|(reader, writer)| {
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

/// Report whether a handle carries `HANDLE_FLAG_INHERIT`, for native
/// regressions.
///
/// This is the Windows analogue of the Unix `FD_CLOEXEC` assertion: it
/// observes the creation policy of an endpoint, not its liveness, and it is
/// test support, so it is neither exported nor compiled into the shipped
/// library.
///
/// # Errors
/// Returns the Win32 error for any failure, including an invalid handle.
/// Folding one into either answer would report an inheritance verdict the
/// observation never established.
#[cfg(test)]
pub(crate) fn handle_is_inheritable(raw: PlatformFd) -> io::Result<bool> {
    let mut flags = 0;
    // SAFETY: GetHandleInformation validates the opaque handle and writes
    // flags only through the live output pointer. No ownership is acquired.
    if unsafe { GetHandleInformation(raw as _, &mut flags) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(flags & HANDLE_FLAG_INHERIT != 0)
}

/// Read once into initialized storage, retaining the handle borrow.
///
/// Native I/O requires the synchronous capability, which a
/// [`SynchronousOwnedStream`] lends:
///
/// ```
/// use std::os::windows::io::AsHandle;
///
/// use cuprum_native_io::{BorrowedStream, pipe, read_once, synchronous_pipe, write_once};
///
/// // A generic handle exists here too; it simply is not passed to `read_once`.
/// // The control imports everything the rejection case below does, so a
/// // renamed import fails this control instead of letting that case pass for
/// // the wrong reason on a rustdoc that ignores the error code.
/// let (generic_reader, _generic_writer) = pipe()?;
/// let _generic: BorrowedStream<'_> = generic_reader.as_handle();
///
/// let (reader, writer) = synchronous_pipe()?;
/// assert_eq!(write_once(writer.as_synchronous_borrowed(), b"x")?, 1);
/// let mut buffer = [0; 1];
/// assert_eq!(read_once(reader.as_synchronous_borrowed(), &mut buffer)?, 1);
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// A live generic handle does not grant that capability. Constructing a
/// genuine overlapped handle is cumbersome and unstable in CI, so this pins
/// the safer, durable guard: generic borrowed handles cannot reach the
/// synchronous adapter through its safe API. `write_once` carries the same
/// guard in its own doctest, so widening either function fails its own case
/// rather than being hidden by the other's diagnostic.
///
/// ```compile_fail,E0308
/// use std::os::windows::io::AsHandle;
///
/// use cuprum_native_io::{BorrowedStream, pipe, read_once};
///
/// let (reader, _writer) = pipe()?;
/// let generic_reader: BorrowedStream<'_> = reader.as_handle();
/// let mut buffer = [0; 1];
/// let _ = read_once(generic_reader, &mut buffer);
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// # Errors
/// Returns the native I/O error, including interruption, without retrying.
pub fn read_once(stream: SynchronousBorrowedStream<'_>, buffer: &mut [u8]) -> io::Result<isize> {
    with_file(stream, |file| file.read(buffer))
}

/// Write once from initialized storage, retaining the handle borrow.
///
/// Native I/O requires the synchronous capability, which a
/// [`SynchronousOwnedStream`] lends:
///
/// ```
/// use std::os::windows::io::AsHandle;
///
/// use cuprum_native_io::{BorrowedStream, pipe, synchronous_pipe, write_once};
///
/// // As on `read_once`, the control shares the rejection case's imports.
/// let (_generic_reader, generic_writer) = pipe()?;
/// let _generic: BorrowedStream<'_> = generic_writer.as_handle();
///
/// let (_reader, writer) = synchronous_pipe()?;
/// assert_eq!(write_once(writer.as_synchronous_borrowed(), b"x")?, 1);
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// A live generic handle does not grant that capability, for the reason given
/// on [`read_once`]:
///
/// ```compile_fail,E0308
/// use std::os::windows::io::AsHandle;
///
/// use cuprum_native_io::{BorrowedStream, pipe, write_once};
///
/// let (_reader, writer) = pipe()?;
/// let generic_writer: BorrowedStream<'_> = writer.as_handle();
/// let _ = write_once(generic_writer, b"x");
/// # Ok::<(), std::io::Error>(())
/// ```
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
