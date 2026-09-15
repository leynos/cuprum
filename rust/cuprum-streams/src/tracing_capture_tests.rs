//! Tests for capture isolation and the harness's own matchers.
//!
//! `event_matches` is what other modules assert their diagnostics with, so a
//! predicate it quietly ignores would weaken every one of those tests at once.
//! The negative cases vary exactly one predicate, while the property below
//! generates registration histories around one shared warning callsite.

use proptest::prelude::*;
use rstest::rstest;
use tracing::Level;
use tracing::callsite::Callsite;

use super::{Captured, capture};

/// Emit one known event and return what the harness captured.
fn captured_probe() -> Captured {
    capture(Level::DEBUG, || {
        tracing::debug!(bytes_transferred = 0_u64, "probe event");
    })
}

/// Emit the shared warning callsite used to vary registration histories.
fn emit_registration_probe(is_parent: bool, sequence: u16) {
    tracing::warn!(
        is_parent,
        sequence = u64::from(sequence),
        "registration probe"
    );
}

/// Let a child emit with either its default subscriber or its own capture.
fn run_child_capture(is_captured: bool, max_level: Level, sequence: u16) -> bool {
    std::thread::spawn(move || {
        if is_captured {
            let _ = capture(max_level, || emit_registration_probe(false, sequence));
        } else {
            emit_registration_probe(false, sequence);
        }
    })
    .join()
    .is_ok()
}

/// Preserve WARN capture after the same callsite was filtered at ERROR.
#[rstest]
fn warn_capture_records_the_same_callsite_after_error_capture() {
    let emit_warning = || tracing::warn!(attempt = 1_u64, "warning probe");

    let error_capture = capture(Level::ERROR, emit_warning);
    assert!(
        error_capture.events.is_empty(),
        "ERROR must filter out WARN"
    );

    let warn_capture = capture(Level::WARN, emit_warning);
    assert!(
        warn_capture.event_matches(Level::WARN, "warning probe", &[("attempt", "1")]),
        "WARN must capture the warning after ERROR used the same callsite",
    );
}

/// Preserve a parent capture after an uncaptured child first uses its callsite.
#[rstest]
fn warn_capture_records_callsite_first_seen_without_subscriber() {
    let emit_warning = || tracing::warn!(attempt = 1_u64, "cross-thread warning probe");
    let captured = capture(Level::WARN, || {
        // The new thread has no default subscriber, but shares the
        // process-global callsite with this thread's active capture.
        let child_result = std::thread::spawn(emit_warning).join();
        assert!(child_result.is_ok(), "the warning probe thread must finish");
        emit_warning();
    });
    assert!(
        captured.event_matches(
            Level::WARN,
            "cross-thread warning probe",
            &[("attempt", "1")],
        ),
        "registration on an uncaptured thread must not disable this capture",
    );
    assert_eq!(captured.events.len(), 1, "only this thread is captured");
}

proptest! {
    /// Preserve every parent warning through generated child registration histories.
    #[test]
    fn generated_registration_histories_preserve_parent_capture(
        parent_max_level in prop_oneof![Just(Level::ERROR), Just(Level::WARN)],
        history in prop::collection::vec(
            (
                any::<bool>(),
                any::<bool>(),
                prop_oneof![Just(Level::ERROR), Just(Level::WARN)],
                any::<u16>(),
            ),
            1..5,
        ),
    ) {
        let mut children_finished = true;
        let captured = capture(parent_max_level, || {
            for (child_first, child_is_captured, child_max_level, sequence) in &history {
                if *child_first {
                    children_finished &=
                        run_child_capture(*child_is_captured, *child_max_level, *sequence);
                }
                emit_registration_probe(true, *sequence);
                if !child_first {
                    children_finished &=
                        run_child_capture(*child_is_captured, *child_max_level, *sequence);
                }
            }
        });

        prop_assert!(children_finished, "each generated child must finish");
        let expected_events = if parent_max_level == Level::WARN {
            history.len()
        } else {
            0
        };
        prop_assert_eq!(captured.events.len(), expected_events);
        if parent_max_level == Level::WARN {
            for (_, _, _, sequence) in history {
                let sequence_text = sequence.to_string();
                prop_assert!(captured.event_matches(
                    Level::WARN,
                    "registration probe",
                    &[("is_parent", "true"), ("sequence", sequence_text.as_str())],
                ));
            }
        }
    }
}

/// Require registration to defer level filtering to each active capture.
#[rstest]
#[case::disabled(Level::ERROR)]
#[case::enabled(Level::WARN)]
fn callsite_registration_keeps_level_filter_dynamic(#[case] max_level: Level) {
    let callsite = tracing::callsite! {
        name: "registration probe",
        kind: tracing::metadata::Kind::EVENT,
        level: Level::WARN,
        fields:
    };
    capture(max_level, || {
        let interest = tracing::dispatcher::get_default(|dispatch| {
            dispatch.register_callsite(callsite.metadata())
        });
        // Dispatch creation rebuilds interest, so sequential captures alone
        // cannot detect a stale registration overwriting a concurrent one.
        assert!(
            interest.is_sometimes(),
            "registration must defer to each capture's enabled check",
        );
    });
}

/// Accept an event whose level, message, and fields all match.
#[rstest]
fn event_matches_accepts_the_exact_event() {
    assert!(
        captured_probe().event_matches(Level::DEBUG, "probe event", &[("bytes_transferred", "0")],),
        "the event that fired must match its own level, message, and fields",
    );
}

/// Reject an event when any one matching predicate differs.
#[rstest]
#[case::wrong_level(Level::WARN, "probe event", "bytes_transferred", "0")]
#[case::wrong_message(Level::DEBUG, "a different event", "bytes_transferred", "0")]
#[case::wrong_value(Level::DEBUG, "probe event", "bytes_transferred", "1")]
#[case::absent_field(Level::DEBUG, "probe event", "no_such_field", "0")]
fn event_matches_rejects_a_single_mismatch(
    #[case] level: Level,
    #[case] message: &str,
    #[case] field: &str,
    #[case] value: &str,
) {
    assert!(
        !captured_probe().event_matches(level, message, &[(field, value)]),
        "event_matches must require every predicate, not just some",
    );
}
