//! Bounded Loom model of the native-pump lifecycle hand-off.
//!
//! Production coordinates the synchronous Rust pump from Python. This module
//! models that cross-thread ownership protocol, while reusing the production
//! borrowed-reader discipline and pump transition machine. It has no production
//! runtime effect: it is available only under `--cfg loom`.

mod sync;

use cuprum_native_io::loom_support::borrowed_reader_close_count as native_borrowed_reader_close_count;
use cuprum_streams::loom_support::{
    drive_downstream_close, drive_failed_pump, drive_successful_pump,
};
use sync::{Arc, AtomicBool, AtomicUsize, Cell, JoinHandle, Mutex, Ordering, thread};

/// Result supplied by the explicit native-I/O environment actor.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeOutcome {
    /// The synchronous Rust pump reaches end of input normally.
    Succeeded,
    /// Downstream closes early, so the pump drains the reader without writing.
    DownstreamClosed,
    /// The native operation returns a terminal error.
    Failed,
}

/// Environment choice for submission before a native worker owns descriptors.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SubmissionOutcome {
    /// The executor accepts the native pump.
    Submitted,
    /// Descriptor duplication or submission fails before ownership moves.
    Failed,
}

/// Stable terminal phase mirrored from the Python cleanup lifecycle.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TerminalState {
    /// No terminal result has been observed.
    Pending,
    /// A worker owns the duplicated native descriptors.
    Submitted,
    /// The worker reached a terminal native outcome.
    WorkerFinished(NativeOutcome),
    /// Callback-owned resources have been released exactly once.
    Released,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DescriptorOwner {
    Callback,
    NativeWorker,
    Closed,
}

/// Snapshot asserted by an external Loom harness after all actors join.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LifecycleSnapshot {
    /// Number of times the duplicate writer was closed.
    pub writer_closes: usize,
    /// Number of times the borrowed reader was closed.
    pub reader_closes: usize,
    /// Whether cancellation reached the event-loop actor.
    pub was_cancelled: bool,
    /// Number of cleanup executions admitted by the cleanup-once guard.
    pub cleanup_count: usize,
    /// Whether cleanup restored the temporary blocking-mode guard.
    pub blocking_restored: bool,
    /// Whether cleanup resumed the paused reader transport.
    pub reader_resumed: bool,
    /// Whether a worker could still use native descriptors at release.
    pub released_while_worker_active: bool,
    /// Whether an observer saw a completed operation after notification.
    pub observer_saw_completion: bool,
    /// Terminal phase after the bounded actors complete.
    pub terminal: TerminalState,
}

struct DescriptorRecord {
    owner: Cell<DescriptorOwner>,
    closes: usize,
}

impl DescriptorRecord {
    fn callback_owned() -> Self {
        Self {
            owner: Cell::new(DescriptorOwner::Callback),
            closes: 0,
        }
    }

    fn hand_to_worker(&self) {
        assert_eq!(self.owner.get(), DescriptorOwner::Callback);
        self.owner.set(DescriptorOwner::NativeWorker);
    }

    fn close_once(&mut self, inject_double_close: bool) {
        if self.owner.get() != DescriptorOwner::Closed {
            self.owner.set(DescriptorOwner::Closed);
            self.closes = self.closes.saturating_add(1);
        }
        if inject_double_close {
            self.closes = self.closes.saturating_add(1);
        }
    }
}

struct Lifecycle {
    terminal: TerminalState,
    cleanup_completed: bool,
    blocking_restored: bool,
    reader_resumed: bool,
    worker_active: bool,
    released_while_worker_active: bool,
    observer_saw_completion: bool,
    writer: DescriptorRecord,
}

impl Lifecycle {
    fn new() -> Self {
        Self {
            terminal: TerminalState::Pending,
            cleanup_completed: false,
            blocking_restored: false,
            reader_resumed: false,
            worker_active: false,
            released_while_worker_active: false,
            observer_saw_completion: false,
            writer: DescriptorRecord::callback_owned(),
        }
    }
}

/// Shared state corresponding to Python's `_RustPumpState` surface.
pub struct NativePumpModel {
    was_cancelled: AtomicBool,
    completion_notified: AtomicBool,
    cleanup_count: AtomicUsize,
    cleanup_lock: Mutex<Lifecycle>,
    inject_double_close: bool,
}

impl NativePumpModel {
    /// Create a model with one callback-owned writer duplicate.
    #[must_use]
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            was_cancelled: AtomicBool::new(false),
            completion_notified: AtomicBool::new(false),
            cleanup_count: AtomicUsize::new(0),
            cleanup_lock: Mutex::new(Lifecycle::new()),
            inject_double_close: false,
        })
    }

    #[cfg(feature = "loom-defect-fixture")]
    /// Create an explicitly selected defective model for non-vacuity evidence.
    #[must_use]
    pub fn with_double_close_defect() -> Arc<Self> {
        Arc::new(Self {
            was_cancelled: AtomicBool::new(false),
            completion_notified: AtomicBool::new(false),
            cleanup_count: AtomicUsize::new(0),
            cleanup_lock: Mutex::new(Lifecycle::new()),
            inject_double_close: true,
        })
    }

    /// Run the event-loop submission actor and return the worker, if accepted.
    pub fn submit(
        model: &Arc<Self>,
        outcome: SubmissionOutcome,
        native: NativeOutcome,
    ) -> Option<JoinHandle<()>> {
        if model.was_cancelled.load(Ordering::Acquire) {
            model.finish_without_worker(NativeOutcome::Failed);
            return None;
        }
        if matches!(outcome, SubmissionOutcome::Failed) {
            model.finish_without_worker(NativeOutcome::Failed);
            return None;
        }
        {
            let mut lifecycle = model.cleanup_lock.lock().expect("model lock poisoned");
            lifecycle.writer.hand_to_worker();
            lifecycle.worker_active = true;
            lifecycle.terminal = TerminalState::Submitted;
        }
        let worker = Arc::clone(model);
        Some(thread::spawn(move || worker.run_worker(native)))
    }

    /// Model a cancellation request from the Python event-loop task.
    pub fn cancel(&self) {
        self.was_cancelled.store(true, Ordering::Release);
        self.complete_if_safe();
    }

    /// Model a completion callback or cleanup waiter observing settlement.
    pub fn observe_completion(&self) {
        if self.completion_notified.load(Ordering::Acquire) {
            let mut lifecycle = self.cleanup_lock.lock().expect("model lock poisoned");
            lifecycle.observer_saw_completion = lifecycle.cleanup_completed;
        }
        self.complete_if_safe();
    }

    /// Return the model's post-join observable state.
    #[must_use]
    pub fn snapshot(&self) -> LifecycleSnapshot {
        let lifecycle = self.cleanup_lock.lock().expect("model lock poisoned");
        LifecycleSnapshot {
            writer_closes: lifecycle.writer.closes,
            reader_closes: borrowed_reader_close_count(),
            was_cancelled: self.was_cancelled.load(Ordering::Acquire),
            cleanup_count: self.cleanup_count.load(Ordering::Acquire),
            blocking_restored: lifecycle.blocking_restored,
            reader_resumed: lifecycle.reader_resumed,
            released_while_worker_active: lifecycle.released_while_worker_active,
            observer_saw_completion: lifecycle.observer_saw_completion,
            terminal: lifecycle.terminal,
        }
    }

    fn run_worker(&self, native: NativeOutcome) {
        drive_production_pump_machine(native);
        {
            let mut lifecycle = self.cleanup_lock.lock().expect("model lock poisoned");
            lifecycle.worker_active = false;
            lifecycle.terminal = TerminalState::WorkerFinished(native);
        }
        self.complete_if_safe();
    }

    fn finish_without_worker(&self, native: NativeOutcome) {
        {
            let mut lifecycle = self.cleanup_lock.lock().expect("model lock poisoned");
            lifecycle.terminal = TerminalState::WorkerFinished(native);
        }
        self.complete_if_safe();
    }

    fn complete_if_safe(&self) {
        let mut lifecycle = self.cleanup_lock.lock().expect("model lock poisoned");
        if lifecycle.cleanup_completed || lifecycle.worker_active {
            return;
        }
        lifecycle.released_while_worker_active = lifecycle.worker_active;
        lifecycle.writer.close_once(self.inject_double_close);
        lifecycle.blocking_restored = true;
        lifecycle.reader_resumed = true;
        lifecycle.cleanup_completed = true;
        lifecycle.terminal = TerminalState::Released;
        self.cleanup_count.fetch_add(1, Ordering::AcqRel);
        self.completion_notified.store(true, Ordering::Release);
    }
}

fn borrowed_reader_close_count() -> usize {
    native_borrowed_reader_close_count()
}

fn drive_production_pump_machine(native: NativeOutcome) {
    match native {
        NativeOutcome::Succeeded => drive_successful_pump(),
        NativeOutcome::DownstreamClosed => drive_downstream_close(),
        NativeOutcome::Failed => drive_failed_pump(),
    }
}
