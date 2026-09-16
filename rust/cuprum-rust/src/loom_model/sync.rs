//! Compatibility primitives for the modelled cross-thread state.

#[cfg(loom)]
pub(super) use loom::cell::Cell;
#[cfg(loom)]
pub(super) use loom::sync::Arc;
#[cfg(loom)]
pub(super) use loom::sync::Mutex;
#[cfg(loom)]
pub(super) use loom::sync::MutexGuard;
#[cfg(loom)]
pub(super) use loom::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
#[cfg(loom)]
pub(super) use loom::thread::{self, JoinHandle};

#[cfg(not(loom))]
pub(super) use std::cell::Cell;
#[cfg(not(loom))]
pub(super) use std::sync::Arc;
#[cfg(not(loom))]
pub(super) use std::sync::Mutex;
#[cfg(not(loom))]
pub(super) use std::sync::MutexGuard;
#[cfg(not(loom))]
pub(super) use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
#[cfg(not(loom))]
pub(super) use std::thread::{self, JoinHandle};
