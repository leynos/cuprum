//! Tests that the pump operation span keeps `warn!`/`error!` context under the
//! production `warn`/`error` tracing filters, not merely under `info`.
//!
//! The read/write seams attach only local fields (`platform`, `error`) to their
//! EINTR `warn!` and fatal-I/O `error!` events; the operation name and
//! `buffer_size` come from the enclosing [`operation_span`]. If that span were
//! created at INFO level it would be disabled whenever a subscriber filters out
//! `info` — a common production configuration — and those events would lose
//! their operation context. These tests pin the span to a level that survives
//! `warn`- and `error`-only filters.

use std::collections::{BTreeSet, HashMap};
use std::io;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use tracing::field::{Field, Visit};
use tracing::span::{Attributes, Id, Record};
use tracing::{Event, Level, Metadata, Subscriber};

use super::{operation_span, read_raw_fd_with, write_all_unix_with};

/// Field names visible to an emitted event: its own fields plus every field
/// carried by the spans currently on the stack.
#[derive(Debug, Clone)]
struct CapturedEvent {
    level: Level,
    fields: BTreeSet<String>,
}

#[derive(Default)]
struct State {
    /// Fields recorded for each live span, keyed by raw span id.
    span_fields: HashMap<u64, BTreeSet<String>>,
    /// Currently entered spans, innermost last.
    stack: Vec<u64>,
    /// Monotonic id source; never zero, which `Id::from_u64` forbids.
    next_id: u64,
    events: Vec<CapturedEvent>,
}

/// Subscriber that mimics a max-level filter and records, for each enabled
/// event, the field names reachable from the active span stack.
struct FilterCapture {
    max_level: Level,
    state: Arc<Mutex<State>>,
}

/// Visitor that collects field names (values are irrelevant to these tests).
struct NameVisitor(BTreeSet<String>);

impl NameVisitor {
    fn note(&mut self, field: &Field) {
        self.0.insert(field.name().to_owned());
    }
}

impl Visit for NameVisitor {
    // Capture the name regardless of which typed path tracing dispatches to, so
    // the assertions do not depend on how each value type is recorded.
    fn record_u64(&mut self, field: &Field, _value: u64) {
        self.note(field);
    }

    fn record_i64(&mut self, field: &Field, _value: i64) {
        self.note(field);
    }

    fn record_str(&mut self, field: &Field, _value: &str) {
        self.note(field);
    }

    fn record_bool(&mut self, field: &Field, _value: bool) {
        self.note(field);
    }

    fn record_debug(&mut self, field: &Field, _value: &dyn std::fmt::Debug) {
        self.note(field);
    }
}

fn lock(state: &Arc<Mutex<State>>) -> MutexGuard<'_, State> {
    state.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Subscriber for FilterCapture {
    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        // `Level` orders ERROR < WARN < INFO < DEBUG < TRACE, so an item is
        // enabled when its level is at or below the configured verbosity.
        *metadata.level() <= self.max_level
    }

    fn new_span(&self, attrs: &Attributes<'_>) -> Id {
        let mut visitor = NameVisitor(BTreeSet::new());
        attrs.record(&mut visitor);
        let mut state = lock(&self.state);
        state.next_id = state.next_id.saturating_add(1);
        let id = state.next_id;
        state.span_fields.insert(id, visitor.0);
        Id::from_u64(id)
    }

    fn record(&self, _span: &Id, _values: &Record<'_>) {}

    fn record_follows_from(&self, _span: &Id, _follows: &Id) {}

    fn event(&self, event: &Event<'_>) {
        let ancestor_fields = {
            let state = lock(&self.state);
            state
                .stack
                .iter()
                .filter_map(|id| state.span_fields.get(id).cloned())
                .flatten()
                .collect::<BTreeSet<String>>()
        };
        let mut visitor = NameVisitor(ancestor_fields);
        event.record(&mut visitor);
        lock(&self.state).events.push(CapturedEvent {
            level: *event.metadata().level(),
            fields: visitor.0,
        });
    }

    fn enter(&self, span: &Id) {
        lock(&self.state).stack.push(span.into_u64());
    }

    fn exit(&self, span: &Id) {
        let mut state = lock(&self.state);
        if let Some(pos) = state.stack.iter().rposition(|&id| id == span.into_u64()) {
            state.stack.remove(pos);
        }
    }
}

/// Drive `body` under a `FilterCapture` limited to `max_level` and return the
/// events it recorded.
fn capture_events(max_level: Level, body: impl FnOnce()) -> Vec<CapturedEvent> {
    let state = Arc::new(Mutex::new(State::default()));
    let subscriber = FilterCapture {
        max_level,
        state: Arc::clone(&state),
    };
    tracing::subscriber::with_default(subscriber, body);
    lock(&state).events.clone()
}

/// True when some captured event at `level` carries the operation context the
/// seams rely on the span to supply.
fn has_operation_context(events: &[CapturedEvent], level: Level) -> bool {
    events.iter().any(|event| {
        event.level == level
            && event.fields.contains("operation")
            && event.fields.contains("buffer_size")
    })
}

#[test]
fn warn_filter_keeps_eintr_warning_context() {
    let events = capture_events(Level::WARN, || {
        let span = operation_span("pump_stream_readwrite", 4096);
        let _guard = span.enter();
        // Interrupt once, then report EOF, so the read seam emits its EINTR
        // `warn!` inside the operation span.
        let mut attempts = 0_u8;
        let outcome = read_raw_fd_with(|| {
            attempts = attempts.saturating_add(1);
            if attempts == 1 {
                return Err(io::Error::from(io::ErrorKind::Interrupted));
            }
            Ok(0)
        });
        assert!(
            outcome.is_ok(),
            "the read seam should retry past the interrupt and reach EOF",
        );
    });

    assert!(
        has_operation_context(&events, Level::WARN),
        "EINTR warn event must retain operation + buffer_size context under a \
         warn filter; captured: {events:?}",
    );
}

#[test]
fn error_filter_keeps_fatal_write_context() {
    let events = capture_events(Level::ERROR, || {
        let span = operation_span("pump_stream_readwrite", 8192);
        let _guard = span.enter();
        // A write that makes zero progress is fatal and emits the seam's
        // `error!` inside the operation span.
        let outcome = write_all_unix_with(b"payload", |_chunk| Ok(0));
        assert!(outcome.is_err(), "a zero-progress write is fatal");
    });

    assert!(
        has_operation_context(&events, Level::ERROR),
        "fatal write error event must retain operation + buffer_size context \
         under an error filter; captured: {events:?}",
    );
}
