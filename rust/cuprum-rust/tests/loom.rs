//! Bounded model checks for the Python-to-native pump ownership hand-off.

#![cfg(loom)]

use _rust_backend_native::loom_model::{
    LifecycleSnapshot, NativeOutcome, NativePumpModel, SubmissionOutcome,
};

fn assert_safe_terminal(snapshot: LifecycleSnapshot) {
    assert_eq!(
        snapshot.writer_closes, 1,
        "the duplicate writer closes once"
    );
    assert_eq!(
        snapshot.reader_closes, 0,
        "the borrowed reader stays borrowed"
    );
    assert_eq!(snapshot.cleanup_count, 1, "cleanup runs at most once");
    assert!(snapshot.blocking_restored, "cleanup restores blocking mode");
    assert!(
        snapshot.reader_resumed,
        "cleanup resumes the reader transport"
    );
    assert!(
        !snapshot.released_while_worker_active,
        "cleanup cannot release a descriptor while native work may use it"
    );
}

fn model_submission_and_cleanup(
    submission: SubmissionOutcome,
    native: NativeOutcome,
    cancel_before_submission: bool,
    cancel_after_submission: bool,
) {
    loom::model(move || {
        let state = NativePumpModel::new();
        if cancel_before_submission {
            state.cancel();
        }
        let worker = NativePumpModel::submit(&state, submission, native);
        if cancel_after_submission {
            state.cancel();
            state.cancel();
        }
        let observer = state.clone();
        let observer = loom::thread::spawn(move || observer.observe_completion());
        if let Some(worker) = worker {
            worker.join().expect("worker actor must finish");
        }
        observer.join().expect("observer actor must finish");
        state.observe_completion();
        assert_safe_terminal(state.snapshot());
    });
}

#[test]
fn writer_handoff_and_cleanup_close_once() {
    model_submission_and_cleanup(
        SubmissionOutcome::Submitted,
        NativeOutcome::Succeeded,
        false,
        false,
    );
}

#[test]
fn failed_handoff_releases_callback_owner_without_a_worker() {
    model_submission_and_cleanup(
        SubmissionOutcome::Failed,
        NativeOutcome::Failed,
        false,
        false,
    );
}

#[test]
fn cancellation_before_after_and_repeated_submission_are_safe() {
    model_submission_and_cleanup(
        SubmissionOutcome::Submitted,
        NativeOutcome::Succeeded,
        true,
        false,
    );
    model_submission_and_cleanup(
        SubmissionOutcome::Submitted,
        NativeOutcome::Succeeded,
        false,
        true,
    );
    model_submission_and_cleanup(
        SubmissionOutcome::Submitted,
        NativeOutcome::Failed,
        false,
        true,
    );
}

#[test]
fn cancellation_before_submission_remains_released() {
    loom::model(|| {
        let state = NativePumpModel::new();
        state.cancel();

        let worker = NativePumpModel::submit(
            &state,
            SubmissionOutcome::Submitted,
            NativeOutcome::Succeeded,
        );

        assert!(worker.is_none(), "cancelled work must not spawn a worker");
        let snapshot = state.snapshot();
        assert_eq!(
            snapshot.terminal,
            _rust_backend_native::loom_model::TerminalState::Released
        );
        assert_safe_terminal(snapshot);
    });
}

#[test]
fn downstream_close_remains_terminal_under_competing_observers() {
    model_submission_and_cleanup(
        SubmissionOutcome::Submitted,
        NativeOutcome::DownstreamClosed,
        false,
        false,
    );
}

#[cfg(feature = "loom-defect-fixture")]
#[test]
#[should_panic(expected = "the duplicate writer closes once")]
fn deliberate_double_close_fixture_is_detected() {
    loom::model(|| {
        let state = NativePumpModel::with_double_close_defect();
        let worker =
            NativePumpModel::submit(&state, SubmissionOutcome::Failed, NativeOutcome::Failed);
        assert!(worker.is_none());
        assert_safe_terminal(state.snapshot());
    });
}
