//! Exercise the Windows CRT reader with real CRT pipe descriptors.
//!
//! Every case goes through [`duplicate_crt_reader`] rather than a raw borrow,
//! so no test can construct a descriptor outside the module's ownership
//! contract. The closed-descriptor case is run in a child process: closing a
//! descriptor number frees it for reuse by any other test in this shared
//! binary, and the hazard under test is a process abort, which only an exit
//! status can report.
//!
//! This is a child module of `windows_crt` rather than a sibling top-level
//! module because it needs that module's private items (`classify_crt_error`,
//! `crt_read_count`, `read_crt_once`) and its `CRT_MAX_BUFFER_SIZE` bound. Its
//! parent keeps the production code, which is what the plan's per-file size
//! limit is protecting; splitting on that seam keeps the tests beside the
//! contract they pin without widening any item's visibility.

use std::{io, mem::MaybeUninit};

use rstest::rstest;
use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_INVALID_HANDLE};

use super::{
    CRT_MAX_BUFFER_SIZE,
    classify_crt_error,
    crt_read_count,
    duplicate_crt_reader,
    read_crt_once,
};

/// Name the ignored case that reads a descriptor it has just closed.
///
/// The parent test re-invokes this same test binary with libtest's own
/// `--exact` and `--ignored` flags, so the child's entry point stays the
/// harness's `main` and no extra binary, argument plumbing, or environment
/// channel is needed. The full path is spelled out because `--exact` matches
/// test names, not module paths.
const CLOSED_DESCRIPTOR_CASE: &str =
    "windows_crt::tests::reports_a_closed_descriptor_as_an_invalid_handle";

/// How long the re-invoked child may take before it is assumed hung.
///
/// The child runs exactly one `--exact` case, so a healthy child finishes in
/// well under a second. The bound exists so a regression that deadlocks the
/// CRT read fails this test instead of stalling the whole binary until an
/// external job timeout notices.
const CHILD_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(60);

struct OwnedCrtDescriptor(i32);

impl Drop for OwnedCrtDescriptor {
    fn drop(&mut self) {
        // SAFETY: this test wrapper uniquely owns the CRT descriptor.
        let _ = unsafe { libc::close(self.0) };
    }
}

/// Release a descriptor and hand back its number, which is now closed.
fn close_and_forget(descriptor: OwnedCrtDescriptor) -> i32 {
    let raw = descriptor.0;
    // SAFETY: this test wrapper uniquely owns the descriptor and closes it
    // exactly once here; the owner is then forgotten so it cannot close the
    // number a second time.
    let closed = unsafe { libc::close(raw) };
    assert_eq!(closed, 0, "the CRT descriptor must close for this test");
    std::mem::forget(descriptor);
    raw
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

/// Write a payload to a CRT descriptor and close it.
fn write_payload_and_close(writer: OwnedCrtDescriptor, payload: &[u8]) {
    // SAFETY: the test owns `writer`; payload remains readable through its
    // complete length for the duration of `_write`.
    let written = unsafe {
        libc::write(
            writer.0,
            payload.as_ptr().cast(),
            u32::try_from(payload.len()).expect("short test payload fits u32"),
        )
    };
    assert_eq!(
        written,
        i32::try_from(payload.len()).expect("fits i32"),
        "the test payload must be written in full",
    );
    drop(writer);
}

/// Wait for a spawned child to exit, killing it if it outlives `CHILD_TIMEOUT`.
///
/// `Child::wait_with_output` has no deadline of its own, so a child that
/// deadlocks would hold this test until the surrounding job timed out. The
/// child is reaped on both paths: a timed-out process is killed and then
/// waited on, so the test never leaves a zombie behind.
fn wait_bounded(
    mut child: std::process::Child,
) -> io::Result<(std::process::ExitStatus, Vec<u8>, Vec<u8>)> {
    let deadline = std::time::Instant::now() + CHILD_TIMEOUT;
    loop {
        if let Some(status) = child.try_wait()? {
            let output = child.wait_with_output()?;
            return Ok((status, output.stdout, output.stderr));
        }
        if std::time::Instant::now() >= deadline {
            child.kill()?;
            let _ = child.wait()?;
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!(
                    "the closed-descriptor child did not finish within {CHILD_TIMEOUT:?} and was \
                     killed",
                ),
            ));
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
}

/// Run the closed-descriptor case in a child and require a clean exit.
///
/// A child that aborted on the read reports a failing status, which is the
/// signal under test. A child that selected no case at all also exits zero,
/// so success has to be distinguished from vacuity: the child's own output
/// must report exactly one passing test.
#[test]
fn closed_descriptor_child_exits_cleanly() {
    let child = std::process::Command::new(
        std::env::current_exe().expect("the test executable path must be available"),
    )
    .args([
        "--exact",
        CLOSED_DESCRIPTOR_CASE,
        "--ignored",
        "--nocapture",
        "--test-threads=1",
    ])
    // Piped rather than inherited: the vacuity check below reads the child's
    // own result line, and an inherited stream would leave both buffers empty.
    .stdout(std::process::Stdio::piped())
    .stderr(std::process::Stdio::piped())
    .spawn()
    .expect("the child test process must start");

    let (status, raw_stdout, raw_stderr) =
        wait_bounded(child).expect("the child test process must finish or be killed");
    let stdout = String::from_utf8_lossy(&raw_stdout);
    let stderr = String::from_utf8_lossy(&raw_stderr);
    assert!(
        status.success(),
        "a closed CRT descriptor must report an error, not abort the child: {status}; \
         stdout={stdout}; stderr={stderr}",
    );
    // A filter that matches nothing also exits zero, so the exit status
    // alone would pass whether or not the case ran. Naming the case in the
    // child's own result line is what distinguishes the two.
    assert!(
        stdout.contains(&format!("{CLOSED_DESCRIPTOR_CASE} ... ok")),
        "the child must report the closed-descriptor case as passing: {stdout}",
    );
}

#[rstest]
fn reads_binary_pipe_bytes_to_eof() {
    let (reader, writer) = crt_pipe().expect("CRT pipe should be created");
    let payload = b"line\r\n\x1aend";
    write_payload_and_close(writer, payload);

    let owned = duplicate_crt_reader(reader.0).expect("a live CRT descriptor must be duplicable");
    let borrowed = owned.as_borrowed();
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
fn closes_the_duplicate_without_closing_the_caller_descriptor() {
    let (reader, writer) = crt_pipe().expect("CRT pipe should be created");
    let payload = b"owned";
    write_payload_and_close(writer, payload);
    let caller = reader.0;

    let owned = duplicate_crt_reader(caller).expect("a live CRT descriptor must be duplicable");
    let mut buffer = [0; 8];
    read_crt_once(owned.as_borrowed(), &mut buffer).expect("the first read should succeed");
    drop(owned);

    // The duplicate is gone; the caller's descriptor is untouched, which is
    // what the ownership contract promises. A second duplicate from the same
    // source proves the source still works and shares the file position.
    let again = duplicate_crt_reader(caller).expect("the caller descriptor must still be open");
    let remaining = read_crt_once(again.as_borrowed(), &mut buffer)
        .expect("the caller descriptor must remain readable");
    assert_eq!(
        remaining, 0,
        "the duplicate must share the caller's file position, got {remaining}",
    );
}

/// Exercise descriptor lifecycle hazards that need a process of their own.
///
/// Closing a descriptor number frees it for reuse by any other test in this
/// shared binary, and one hazard under test is a process abort, which only
/// an exit status can report. The case is therefore `#[ignore]`d and driven
/// by [`closed_descriptor_child_exits_cleanly`], which proves the child ran
/// this case rather than passing because it selected nothing.
#[test]
#[ignore = "re-invoked as a child by closed_descriptor_child_exits_cleanly"]
fn reports_a_closed_descriptor_as_an_invalid_handle() {
    reports_a_closed_descriptor();
    reads_the_duplicate_after_the_caller_number_is_reused();
}

/// A descriptor number this test has closed must fail instead of aborting.
fn reports_a_closed_descriptor() {
    let (reader, writer) = crt_pipe().expect("CRT pipe should be created");
    drop(writer);
    let closed = close_and_forget(reader);

    let error = duplicate_crt_reader(closed)
        .expect_err("a closed CRT descriptor must report an error, not abort");

    assert_eq!(
        error.raw_os_error(),
        i32::try_from(ERROR_INVALID_HANDLE).ok(),
        "a closed CRT descriptor must map to the invalid-handle code, got {error}",
    );
}

/// The duplicate must stay bound to the resource it was taken from.
///
/// Duplication is one `_dup` inside [`duplicate_crt_reader`], so everything the
/// reader does afterwards goes through the returned descriptor. This closes the
/// caller's number and then lets the CRT hand that same number to a second pipe,
/// which is the strongest form of rebinding available without adding a hook
/// between validation and duplication. A reader that resolved the caller's
/// number a second time would return the second pipe's bytes here.
fn reads_the_duplicate_after_the_caller_number_is_reused() {
    let (reader, writer) = crt_pipe().expect("CRT pipe should be created");
    write_payload_and_close(writer, b"first");
    let owned = duplicate_crt_reader(reader.0).expect("a live CRT descriptor must be duplicable");
    let freed = close_and_forget(reader);

    let (reused_reader, reused_writer) = crt_pipe().expect("second CRT pipe should be created");
    write_payload_and_close(reused_writer, b"other");

    let mut buffer = [0; 8];
    let count = read_crt_once(owned.as_borrowed(), &mut buffer).expect("the duplicate must read");
    let read = buffer
        .get(..usize::try_from(count).expect("count fits usize"))
        .expect("the CRT cannot report more bytes than the buffer holds");
    // The message reports both numbers because whether the CRT happened to
    // hand back the freed one is not something this test may assume; when it
    // did, a second resolution would have read the second pipe's bytes.
    assert_eq!(
        read, b"first",
        "the duplicate must read the resource it was duplicated from; the caller's number was \
         {freed} and the second pipe's reader is {}, so reading {read:?} would mean the reader \
         resolved a number again rather than holding its own descriptor",
        reused_reader.0,
    );
}

#[rstest]
#[case::ebadf_becomes_invalid_handle(0, 0, libc::EBADF, ERROR_INVALID_HANDLE)]
// `_read` on an open but unreadable descriptor fails inside `ReadFile`,
// which sets `ERROR_ACCESS_DENIED`, and the CRT then maps that to `EACCES`.
// Both channels are populated, and the Win32 code is the one reported.
#[case::readfile_access_denied(0, ERROR_ACCESS_DENIED, libc::EACCES, ERROR_ACCESS_DENIED)]
#[case::dos_error_wins_over_win32(5, 6, libc::EBADF, 5)]
#[case::win32_error_wins_over_crt_errno(0, 6, libc::EBADF, 6)]
fn classifies_crt_failure(
    #[case] dos_error: i32,
    #[case] win32_error: u32,
    #[case] crt_errno: i32,
    #[case] expected_native: u32,
) {
    let error = classify_crt_error(dos_error, win32_error, crt_errno);
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
    let error = classify_crt_error(dos_error, win32_error, crt_errno);
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
fn reports_a_write_only_descriptor_instead_of_terminating_the_process() {
    let (reader, writer) = crt_pipe().expect("CRT pipe should be created");
    drop(reader);
    // The descriptor is open, so no range or ownership check can reject it;
    // it is simply not readable. Reading it is the misuse under test, and
    // the installed handler must turn the CRT's rejection into an error.
    let owned = duplicate_crt_reader(writer.0)
        .expect("a write-only descriptor is still a valid descriptor");
    let mut buffer = [0; 8];
    let error = read_crt_once(owned.as_borrowed(), &mut buffer)
        .expect_err("a write-only descriptor must report an error, not abort");

    assert_eq!(
        error.raw_os_error(),
        i32::try_from(ERROR_ACCESS_DENIED).ok(),
        "an unreadable CRT descriptor must report the Win32 access-denied code from the failed \
         read, not the closed-descriptor mapping and not the CRT errno, got {error}",
    );
}
