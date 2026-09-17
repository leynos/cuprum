//! Shared tracing capture harness for tests.
//!
//! Installs a subscriber that mimics a max-level filter and records both the
//! field names visible to each emitted event (its own fields plus every
//! enclosing span's) and the final field values recorded on each span
//! (including values supplied after creation via `Span::record`). Tests use it
//! to assert that `warn!`/`error!` events keep operation context under
//! production filters and that the pump/consume loops record `total_bytes` and
//! retry counts on their span.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Debug;
use std::sync::{Arc, LazyLock, Mutex, MutexGuard, PoisonError};

use tracing::field::{Field, Visit};
use tracing::level_filters::LevelFilter;
use tracing::span::{Attributes, Id, Record};
use tracing::subscriber::{Interest, NoSubscriber};
use tracing::{Dispatch, Event, Level, Metadata, Subscriber};

/// A captured event: its level, the field names reachable from the active span
/// stack when it was emitted, and the values it carried itself.
///
/// `fields` and `values` answer different questions. `fields` includes names
/// inherited from enclosing spans, so it shows whether an event kept its
/// operation context. `values` holds only what the event recorded directly —
/// including its message, which `tracing` stores under the `message` field —
/// so it can identify *which* event fired rather than merely that some event
/// carried a given field name.
#[derive(Debug, Clone)]
pub(crate) struct CapturedEvent {
    pub(crate) level: Level,
    pub(crate) fields: BTreeSet<String>,
    pub(crate) values: BTreeMap<String, String>,
}

/// The result of a capture run.
pub(crate) struct Captured {
    events: Vec<CapturedEvent>,
    /// Final field values for every span opened during the run.
    spans: Vec<BTreeMap<String, String>>,
}

impl Captured {
    /// True when some event at `level` carried every field in `fields`.
    pub(crate) fn event_has_fields(&self, level: Level, fields: &[&str]) -> bool {
        self.events
            .iter()
            .any(|event| event.level == level && fields.iter().all(|f| event.fields.contains(*f)))
    }

    /// True when some event at `level` carried exactly `message` and every
    /// listed field/value pair.
    ///
    /// Stricter than [`Self::event_has_fields`], which any event carrying the
    /// named fields satisfies: this pins the specific event, so an unrelated
    /// one cannot stand in for a missing or malformed diagnostic.
    pub(crate) fn event_matches(
        &self,
        level: Level,
        message: &str,
        values: &[(&str, &str)],
    ) -> bool {
        self.events.iter().any(|event| {
            event.level == level
                && event.values.get("message").map(String::as_str) == Some(message)
                && values.iter().all(|(name, value)| {
                    event.values.get(*name).map(String::as_str) == Some(*value)
                })
        })
    }

    /// The recorded value of `field` on the span whose `operation` field equals
    /// `operation`, if any.
    pub(crate) fn span_field(&self, operation: &str, field: &str) -> Option<String> {
        self.spans
            .iter()
            .find(|span| span.get("operation").map(String::as_str) == Some(operation))
            .and_then(|span| span.get(field).cloned())
    }
}

/// Mutable state shared between the subscriber and the returned [`Captured`].
#[derive(Default)]
struct CaptureState {
    span_fields: HashMap<u64, BTreeMap<String, String>>,
    stack: Vec<u64>,
    next_id: u64,
    events: Vec<CapturedEvent>,
}

/// Visitor that records each field's name and stringified value into a map.
struct FieldVisitor<'a>(&'a mut BTreeMap<String, String>);

impl Visit for FieldVisitor<'_> {
    fn record_u64(&mut self, field: &Field, value: u64) {
        self.0.insert(field.name().to_owned(), value.to_string());
    }

    fn record_i64(&mut self, field: &Field, value: i64) {
        self.0.insert(field.name().to_owned(), value.to_string());
    }

    fn record_str(&mut self, field: &Field, value: &str) {
        self.0.insert(field.name().to_owned(), value.to_owned());
    }

    fn record_bool(&mut self, field: &Field, value: bool) {
        self.0.insert(field.name().to_owned(), value.to_string());
    }

    fn record_debug(&mut self, field: &Field, value: &dyn Debug) {
        self.0.insert(field.name().to_owned(), format!("{value:?}"));
    }
}

/// Subscriber that records events and span fields up to a maximum level.
struct FilterCapture {
    max_level: Level,
    state: Arc<Mutex<CaptureState>>,
}

/// Lock the shared state, recovering the guard if a previous holder panicked.
fn lock(state: &Arc<Mutex<CaptureState>>) -> MutexGuard<'_, CaptureState> {
    state.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Subscriber for FilterCapture {
    /// Request an `enabled` check for every event at this callsite.
    fn register_callsite(&self, _metadata: &'static Metadata<'static>) -> Interest {
        Interest::sometimes()
    }

    /// Accept metadata at or below this capture's configured level.
    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        // `Level` orders ERROR < WARN < INFO < DEBUG < TRACE, so an item is
        // enabled when its level is at or below the configured verbosity.
        *metadata.level() <= self.max_level
    }

    /// Allocate a span identifier and retain its initial fields.
    fn new_span(&self, attrs: &Attributes<'_>) -> Id {
        let mut fields = BTreeMap::new();
        attrs.record(&mut FieldVisitor(&mut fields));
        let mut state = lock(&self.state);
        state.next_id = state.next_id.saturating_add(1);
        let id = state.next_id;
        state.span_fields.insert(id, fields);
        Id::from_u64(id)
    }

    /// Merge fields recorded after a span's creation into its retained state.
    fn record(&self, span: &Id, values: &Record<'_>) {
        let mut fields = BTreeMap::new();
        values.record(&mut FieldVisitor(&mut fields));
        let mut state = lock(&self.state);
        if let Some(existing) = state.span_fields.get_mut(&span.into_u64()) {
            existing.extend(fields);
        }
    }

    /// Ignore causal links because capture assertions inspect only active spans.
    fn record_follows_from(&self, _span: &Id, _follows: &Id) {}

    /// Record an event with its direct and active-span fields.
    fn event(&self, event: &Event<'_>) {
        let mut fields = {
            let state = lock(&self.state);
            state
                .stack
                .iter()
                .filter_map(|id| state.span_fields.get(id))
                .flat_map(|span| span.keys().cloned())
                .collect::<BTreeSet<String>>()
        };
        let mut own = BTreeMap::new();
        event.record(&mut FieldVisitor(&mut own));
        fields.extend(own.keys().cloned());
        lock(&self.state).events.push(CapturedEvent {
            level: *event.metadata().level(),
            fields,
            values: own,
        });
    }

    /// Add the entered span to the active context stack.
    fn enter(&self, span: &Id) {
        lock(&self.state).stack.push(span.into_u64());
    }

    /// Remove the most recently entered matching span from the context stack.
    fn exit(&self, span: &Id) {
        let mut state = lock(&self.state);
        if let Some(pos) = state.stack.iter().rposition(|&id| id == span.into_u64()) {
            state.stack.remove(pos);
        }
    }
}

/// Keep registration on tracing's synchronized registry path for all threads.
///
/// Each capture registers beside this retained dispatch, avoiding the
/// single-dispatch fast path that consults only the registering thread's
/// default subscriber. The guard is never installed as a default subscriber.
static REGISTRY_GUARD: LazyLock<Dispatch> = LazyLock::new(|| Dispatch::new(DormantSubscriber));

/// Disable tracing until the first capture has joined the shared registry.
struct DormantSubscriber;

impl Subscriber for DormantSubscriber {
    /// Keep the retained guard dormant so it never receives events.
    fn enabled(&self, _metadata: &Metadata<'_>) -> bool {
        false
    }

    /// Disable callsites while this is the registry's only dispatch.
    fn max_level_hint(&self) -> Option<LevelFilter> {
        // NoSubscriber alone reports no hint, enabling callsite registration
        // while this guard is still the only dispatch in the registry.
        Some(LevelFilter::OFF)
    }

    /// Produce inert span identifiers because the guard retains no span state.
    fn new_span(&self, attrs: &Attributes<'_>) -> Id {
        NoSubscriber::new().new_span(attrs)
    }

    /// Ignore records because the dormant guard retains no span state.
    fn record(&self, _span: &Id, _values: &Record<'_>) {}

    /// Ignore causal links because the dormant guard retains no span state.
    fn record_follows_from(&self, _span: &Id, _follows: &Id) {}

    /// Discard events because the retained guard is never a test capture.
    fn event(&self, _event: &Event<'_>) {}

    /// Ignore span entry because the dormant guard retains no context.
    fn enter(&self, _span: &Id) {}

    /// Ignore span exit because the dormant guard retains no context.
    fn exit(&self, _span: &Id) {}
}

/// Run `body` under a subscriber limited to `max_level` and return what it
/// captured.
///
/// `FilterCapture` returns `Interest::sometimes()` from `register_callsite`,
/// so tracing re-evaluates `enabled()` for every event and span. A retained,
/// dormant dispatch also makes registration consult tracing's synchronized
/// registry rather than only the registering thread's default subscriber.
/// Together these prevent the process-global callsite cache from retaining
/// another capture's max-level verdict or an uncaptured thread's `never`.
/// Correctness no longer depends on process isolation, although `cargo nextest`
/// remains the project's test runner.
pub(crate) fn capture(max_level: Level, body: impl FnOnce()) -> Captured {
    let _ = LazyLock::force(&REGISTRY_GUARD);
    let state = Arc::new(Mutex::new(CaptureState::default()));
    let subscriber = FilterCapture {
        max_level,
        state: Arc::clone(&state),
    };
    tracing::subscriber::with_default(subscriber, body);
    let guard = lock(&state);
    Captured {
        events: guard.events.clone(),
        spans: guard.span_fields.values().cloned().collect(),
    }
}

#[cfg(test)]
#[path = "tracing_capture_tests.rs"]
mod tests;
