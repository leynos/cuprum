//! Actual resource effects and unwind regressions for the ownership boundary.

#[cfg(unix)]
use std::os::fd::{AsRawFd, IntoRawFd};
#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{LockResult, Mutex, MutexGuard},
};

use rstest::{fixture, rstest};

#[cfg(unix)]
use super::{adopt_writer, borrow, borrow_reader, pipe, with_owned_writer};
use super::{fd_is_open, read_once};
#[cfg(windows)]
use super::{synchronous_pipe, write_once};

// Closed-descriptor observations must not race this test binary's next pipe.
// The guard is outside catch_unwind, so intentional panics do not poison it.
static DESCRIPTOR_TESTS: Mutex<()> = Mutex::new(());

// `fn_single_line` in rustfmt 1.9.0-nightly turns this rstest fixture into a
// form that triggers `unused_braces` under Rust 1.85. Remove this skip when
// that formatter/rstest combination compiles the configured profile cleanly.
#[rustfmt::skip]
#[fixture]
fn descriptor_guard() -> LockResult<MutexGuard<'static, ()>> {
    DESCRIPTOR_TESTS.lock()
}

/// Observe a descriptor, failing loudly when observation itself fails.
///
/// An observation error is neither an open nor a closed verdict: the kernel, or
/// the Win32 call, refused to answer. Folding that into either branch would
/// report an ownership assertion the observation never established, so the
/// test fails here first, naming the underlying error.
fn descriptor_is_open(raw: super::PlatformFd) -> bool {
    match fd_is_open(raw) {
        Ok(is_open) => is_open,
        Err(error) => panic!("descriptor observation failed: {error:?}"),
    }
}

#[rstest]
#[case::normal(false)]
#[case::real_unwind(true)]
#[cfg(unix)]
fn borrowed_reader_survives(
    #[from(descriptor_guard)] guard_result: LockResult<MutexGuard<'static, ()>>,
    #[case] should_panic: bool,
) {
    let guard = guard_result.unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
    let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    let raw = raw_of(&reader);
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: reader remains owned outside this scope, including unwind.
        let view = unsafe { borrow_reader(raw) };
        // Windows also reconstructs a temporary capability File for I/O.
        // Unwind through that exact adapter before observing the real handle.
        #[cfg(windows)]
        if should_panic {
            drop(super::windows::with_file(view, |_| {
                panic!("injected real unwind")
            }));
        }
        assert!(!should_panic, "injected real unwind");
        let written = super::write_once(borrow(&writer), b"ping")
            .unwrap_or_else(|error| panic!("write payload: {error:?}"));
        assert_eq!(written, 4);
        drop(writer);
        let mut buffer = [0; 8];
        let count = read_once(view, &mut buffer)
            .unwrap_or_else(|error| panic!("read through borrow: {error:?}"));
        assert_eq!(count, 4);
        assert_eq!(buffer.get(..4), Some(b"ping".as_slice()));
    }));
    assert_eq!(outcome.is_err(), should_panic);
    assert!(
        descriptor_is_open(raw),
        "borrow must not close the caller's reader"
    );
    drop(reader);
    assert!(
        !descriptor_is_open(raw),
        "the actual owner still closes the descriptor"
    );
    drop(guard);
}

#[rstest]
#[case::normal(0)]
#[case::error(1)]
#[case::real_unwind(2)]
#[cfg(unix)]
fn transferred_writer_closes_and_delivers_eof(
    #[from(descriptor_guard)] guard_result: LockResult<MutexGuard<'static, ()>>,
    #[case] exit: u8,
) {
    let guard = guard_result.unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
    let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    let raw = into_raw(writer);
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: into_raw_fd relinquished the only owner immediately above.
        let owned = unsafe { adopt_writer(raw) };
        with_owned_writer(&reader, owned, |source, sink| {
            assert_eq!(
                super::write_once(borrow(sink), b"pong")
                    .unwrap_or_else(|error| panic!("write: {error:?}")),
                4
            );
            assert!(descriptor_is_open(raw_of(source)));
            match exit {
                2 => panic!("injected real unwind"),
                1 => Err("injected operation error"),
                _ => Ok(()),
            }
        })
    }));
    assert_eq!(outcome.is_err(), exit == 2);
    if exit == 1 {
        assert_eq!(
            outcome.unwrap_or_else(|error| panic!("no panic: {error:?}")),
            Err("injected operation error")
        );
    }
    assert!(
        !descriptor_is_open(raw),
        "transferred writer must close on every exit"
    );
    let mut bytes = [0; 8];
    assert_eq!(
        read_once(borrow(&reader), &mut bytes)
            .unwrap_or_else(|error| panic!("read payload: {error:?}")),
        4
    );
    assert_eq!(bytes.get(..4), Some(b"pong".as_slice()));
    assert_eq!(
        read_once(borrow(&reader), &mut bytes)
            .unwrap_or_else(|error| panic!("read EOF: {error:?}")),
        0
    );
    // A subsequent resource must not be closed by delayed ownership cleanup.
    let (replacement, _replacement_writer) =
        pipe().unwrap_or_else(|error| panic!("replacement pipe: {error:?}"));
    assert!(descriptor_is_open(raw_of(&replacement)));
    drop(guard);
}

#[rstest]
#[cfg(unix)]
fn owned_descriptor_reads_and_closes(
    #[from(descriptor_guard)] guard_result: LockResult<MutexGuard<'static, ()>>,
) {
    let guard = guard_result.unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
    let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    assert_eq!(
        super::write_once(borrow(&writer), b"pong")
            .unwrap_or_else(|error| panic!("write: {error:?}")),
        4
    );
    drop(writer);
    let raw = into_raw(reader);
    // SAFETY: the sole owner was consumed immediately above.
    let owned = unsafe { adopt_writer(raw) };
    let mut bytes = [0; 8];
    assert_eq!(
        read_once(borrow(&owned), &mut bytes).unwrap_or_else(|error| panic!("read: {error:?}")),
        4
    );
    assert_eq!(bytes.get(..4), Some(b"pong".as_slice()));
    drop(owned);
    assert!(!descriptor_is_open(raw));
    drop(guard);
}

#[cfg(unix)]
fn raw_of(resource: &super::OwnedStream) -> super::PlatformFd {
    #[cfg(unix)]
    {
        resource.as_raw_fd()
    }
    #[cfg(windows)]
    {
        resource.as_raw_handle() as usize
    }
}

#[cfg(unix)]
fn into_raw(resource: super::OwnedStream) -> super::PlatformFd {
    #[cfg(unix)]
    {
        resource.into_raw_fd()
    }
    #[cfg(windows)]
    {
        resource.into_raw_handle() as usize
    }
}

#[cfg(windows)]
fn raw_of(resource: &impl super::AsStream) -> super::PlatformFd {
    resource.as_handle().as_raw_handle() as usize
}

#[rstest]
#[case::normal(false)]
#[case::real_unwind(true)]
#[cfg(windows)]
fn synchronous_borrow_survives(
    #[from(descriptor_guard)] guard_result: LockResult<MutexGuard<'static, ()>>,
    #[case] should_panic: bool,
) {
    let guard = guard_result.unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
    let (reader, writer) =
        synchronous_pipe().unwrap_or_else(|error| panic!("create synchronous pipe: {error:?}"));
    let reader_raw = raw_of(&reader);
    let writer_raw = raw_of(&writer);
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        let reader_view = reader.as_synchronous_borrowed();
        if should_panic {
            drop(super::windows::with_file(reader_view, |_| {
                panic!("injected real unwind")
            }));
        }
        assert!(!should_panic, "injected real unwind");
        assert_eq!(
            write_once(writer.as_synchronous_borrowed(), b"ping")
                .unwrap_or_else(|error| panic!("write payload: {error:?}")),
            4
        );
        drop(writer);
        let mut buffer = [0; 8];
        assert_eq!(
            read_once(reader_view, &mut buffer)
                .unwrap_or_else(|error| panic!("read through capability: {error:?}")),
            4
        );
        assert_eq!(buffer.get(..4), Some(b"ping".as_slice()));
    }));
    assert_eq!(outcome.is_err(), should_panic);
    assert!(
        descriptor_is_open(reader_raw),
        "borrowing must not close the caller's reader"
    );
    if !should_panic {
        assert!(
            !descriptor_is_open(writer_raw),
            "the synchronous writer must close when its owner drops"
        );
    }
    drop(reader);
    assert!(
        !descriptor_is_open(reader_raw),
        "the actual owner still closes the reader"
    );
    drop(guard);
}
