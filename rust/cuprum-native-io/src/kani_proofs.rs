//! Bounded proofs over production kernels, with explicit reachability checks.
//!
//! No OS calls occur here. Drop counters witness Rust drop elaboration, not
//! kernel close effects; early errors are not claims about actual unwind.

use core::cell::Cell;

use super::{
    progress::{checked_count, checked_progress},
    with_owned_writer,
};

struct Resource<'a>(&'a Cell<u8>);

impl Drop for Resource<'_> {
    fn drop(&mut self) {
        self.0.set(self.0.get() + 1);
    }
}

#[kani::proof]
fn production_scope_preserves_reader_and_drops_writer() {
    let reader_closes = Cell::new(0);
    let writer_closes = Cell::new(0);
    let reader = Resource(&reader_closes);
    let fail: bool = kani::any();
    let result = with_owned_writer(&reader, Resource(&writer_closes), |_reader, _writer| {
        if fail { Err(()) } else { Ok(()) }
    });
    kani::cover!(result.is_err(), "operation error is reachable");
    kani::cover!(result.is_ok(), "normal completion is reachable");
    assert_eq!(reader_closes.get(), 0);
    assert_eq!(writer_closes.get(), 1);
    drop(reader);
    assert_eq!(reader_closes.get(), 1);
}

#[kani::proof]
fn counts_stay_in_bounds() {
    let count: usize = kani::any();
    let capacity: usize = kani::any();
    let result = checked_count(count, capacity);
    assert_eq!(result.is_some(), count <= capacity);
    if let Some(value) = result {
        assert_eq!(value, count);
    }
    kani::cover!(count == 0 && result.is_some(), "EOF");
    kani::cover!(
        count > 0 && count < capacity && result.is_some(),
        "short IO"
    );
    kani::cover!(result.is_none(), "invalid external count rejected");
}

#[kani::proof]
fn progress_is_exact_or_rejected() {
    let total: u64 = kani::any();
    let remaining: u64 = kani::any();
    let written: u64 = kani::any();
    let result = checked_progress(total, remaining, written);
    assert_eq!(
        result.is_some(),
        written <= remaining && u128::from(total) + u128::from(written) <= u128::from(u64::MAX)
    );
    if let Some((new_total, tail)) = result {
        assert_eq!(
            u128::from(new_total),
            u128::from(total) + u128::from(written)
        );
        assert_eq!(
            u128::from(tail) + u128::from(written),
            u128::from(remaining)
        );
    }
    kani::cover!(result.is_some() && written > 0, "positive progress");
    kani::cover!(
        result.is_none() && written <= remaining,
        "overflow rejection"
    );
    kani::cover!(
        result.is_none() && written > remaining,
        "invalid length rejection"
    );
}
