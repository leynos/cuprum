//! Production owner-scope kernels shared with Kani and unwind tests.
#![forbid(unsafe_code)]

/// Run an operation with a borrowed reader and a scoped owned writer.
///
/// The operation may return an error or unwind. Rust drops `writer` in every
/// returning/unwinding path, while this function cannot drop `reader`'s owner.
/// The shared writer borrow prevents replacing or moving out an `OwnedStream`
/// through the callback. Process abort and deliberately leaking resources are
/// outside this contract.
pub fn with_owned_writer<R, W, T>(reader: &R, writer: W, operation: impl FnOnce(&R, &W) -> T) -> T {
    let result = operation(reader, &writer);
    drop(writer);
    result
}

/// Retain an externally owned value on return, error, and real unwind.
///
/// Only the Windows read/write adapter reconstructs an owner for this helper;
/// it never exposes the owner to external callbacks that could move it out.
#[cfg(any(windows, test, kani))]
pub(crate) fn with_retained_owner<T, R>(value: T, operation: impl FnOnce(&mut T) -> R) -> R {
    let mut retained = core::mem::ManuallyDrop::new(value);
    operation(&mut retained)
}

#[cfg(test)]
mod tests {
    //! Real unwinding through the production retention frame.
    use super::with_retained_owner;
    use core::cell::Cell;
    use std::panic::{AssertUnwindSafe, catch_unwind};

    struct Close<'a>(&'a Cell<u32>);
    impl Drop for Close<'_> {
        fn drop(&mut self) {
            self.0.set(self.0.get() + 1);
        }
    }

    #[test]
    fn retained_owner_survives_real_unwind() {
        let closes = Cell::new(0);
        let outcome = catch_unwind(AssertUnwindSafe(|| {
            with_retained_owner(Close(&closes), |_| panic!("real unwind"));
        }));
        assert!(outcome.is_err());
        assert_eq!(closes.get(), 0);
    }
}
