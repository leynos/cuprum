//! Bounded model checks for the Python-to-native pump ownership hand-off.

#![cfg(loom)]

use _rust_backend_native::loom_model::{
    LifecycleSnapshot,
    NativeOutcome,
    NativePumpModel,
    SubmissionOutcome,
};

fn model(action: impl Fn() + Send + Sync + 'static) {
    let max_threads = std::env::var("LOOM_MAX_THREADS")
        .expect("the Loom driver must set LOOM_MAX_THREADS")
        .parse::<usize>()
        .expect("LOOM_MAX_THREADS must be an unsigned integer");
    assert!(
        (1..=loom::MAX_THREADS).contains(&max_threads),
        "LOOM_MAX_THREADS must be between one and Loom's maximum"
    );
    let mut builder = loom::model::Builder::new();
    builder.max_threads = max_threads;
    builder.check(action);
}

fn assert_safe_terminal(snapshot: LifecycleSnapshot, native_writer_closes: usize) {
    assert_eq!(
        snapshot.callback_writer_closes, 1,
        "the callback-owned state writer closes once"
    );
    assert_eq!(
        snapshot.native_writer_closes, native_writer_closes,
        "the native worker writer closes once after a successful hand-off"
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
    assert!(
        snapshot.observer_saw_completion,
        "the completion observer must receive the worker settlement notification"
    );
}

fn model_submission_and_cleanup(
    submission: SubmissionOutcome,
    native: NativeOutcome,
    cancel_before_submission: bool,
    cancel_after_submission: bool,
) {
    model(move || {
        let state = NativePumpModel::new();
        if cancel_before_submission {
            state
                .cancel()
                .expect("event-loop cancellation must be modelled");
        }
        let worker = NativePumpModel::submit(&state, submission, native)
            .expect("submission model must preserve lifecycle state");
        if cancel_after_submission {
            state.cancel().expect("first cancellation must be modelled");
            state
                .cancel()
                .expect("repeated cancellation must be modelled");
        }
        let observer = state.clone();
        let observer = loom::thread::spawn(move || observer.observe_completion());
        if let Some(worker) = worker {
            worker
                .join()
                .expect("worker actor must finish")
                .expect("worker actor must preserve lifecycle state");
        }
        observer
            .join()
            .expect("observer actor must finish")
            .expect("observer actor must preserve lifecycle state");
        let expected_native_writer_closes = usize::from(
            matches!(submission, SubmissionOutcome::Submitted) && !cancel_before_submission,
        );
        assert_safe_terminal(
            state
                .snapshot()
                .expect("snapshot must observe the settled lifecycle"),
            expected_native_writer_closes,
        );
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
    model(|| {
        let state = NativePumpModel::new();
        state
            .cancel()
            .expect("event-loop cancellation must be modelled");

        let worker = NativePumpModel::submit(
            &state,
            SubmissionOutcome::Submitted,
            NativeOutcome::Succeeded,
        )
        .expect("submission model must preserve lifecycle state");

        assert!(worker.is_none(), "cancelled work must not spawn a worker");
        let observer = state.clone();
        loom::thread::spawn(move || observer.observe_completion())
            .join()
            .expect("observer actor must finish")
            .expect("observer actor must preserve lifecycle state");
        let snapshot = state
            .snapshot()
            .expect("snapshot must observe the settled lifecycle");
        assert_eq!(
            snapshot.terminal,
            _rust_backend_native::loom_model::TerminalState::Released
        );
        assert_safe_terminal(snapshot, 0);

        let failed_submission =
            NativePumpModel::submit(&state, SubmissionOutcome::Failed, NativeOutcome::Failed)
                .expect("failed submission must preserve lifecycle state");
        assert!(
            failed_submission.is_none(),
            "failed submission spawns no worker"
        );
        assert_eq!(
            state
                .snapshot()
                .expect("snapshot must observe the settled lifecycle")
                .terminal,
            _rust_backend_native::loom_model::TerminalState::Released
        );
    });
}

#[test]
fn downstream_close_remains_terminal_under_competing_observers() {
    model(|| {
        let state = NativePumpModel::new();
        let worker = NativePumpModel::submit(
            &state,
            SubmissionOutcome::Submitted,
            NativeOutcome::DownstreamClosed,
        )
        .expect("submission model must preserve lifecycle state")
        .expect("downstream-close outcome must start a worker");
        worker
            .join()
            .expect("worker actor must finish")
            .expect("worker actor must preserve lifecycle state");

        let first_observer = state.clone();
        let first_observer = loom::thread::spawn(move || first_observer.observe_completion());
        let second_observer = state.clone();
        let second_observer = loom::thread::spawn(move || second_observer.observe_completion());
        first_observer
            .join()
            .expect("first observer actor must finish")
            .expect("first observer actor must preserve lifecycle state");
        second_observer
            .join()
            .expect("second observer actor must finish")
            .expect("second observer actor must preserve lifecycle state");
        assert_safe_terminal(
            state
                .snapshot()
                .expect("snapshot must observe the settled lifecycle"),
            1,
        );
    });
}

#[cfg(feature = "loom-defect-fixture")]
#[test]
#[should_panic(expected = "the native worker writer closes once")]
fn deliberate_double_close_fixture_is_detected() {
    model(|| {
        let state = NativePumpModel::with_double_close_defect();
        let worker =
            NativePumpModel::submit(&state, SubmissionOutcome::Submitted, NativeOutcome::Failed)
                .expect("submission must preserve lifecycle state")
                .expect("submitted work must start a worker");
        worker
            .join()
            .expect("worker actor must finish")
            .expect("worker actor must preserve lifecycle state");
        state
            .observe_completion()
            .expect("observer actor must preserve lifecycle state");
        assert_safe_terminal(
            state
                .snapshot()
                .expect("snapshot must observe the settled lifecycle"),
            1,
        );
    });
}

#[cfg(feature = "loom-defect-fixture")]
#[test]
#[should_panic(expected = "cleanup cannot release a descriptor while native work may use it")]
fn deliberate_early_release_fixture_is_detected() {
    model(|| {
        let state = NativePumpModel::with_early_release_defect();
        state
            .release_while_worker_active()
            .expect("the deliberate defect must preserve model state");
        let snapshot = state
            .snapshot()
            .expect("snapshot must observe the deliberate defect");
        assert!(
            !snapshot.released_while_worker_active,
            "cleanup cannot release a descriptor while native work may use it"
        );
    });
}

#[test]
fn cancellation_and_submission_share_the_handoff_linearization_point() {
    model(|| {
        let state = NativePumpModel::new();
        let submitting_state = state.clone();
        let submitter = loom::thread::spawn(move || {
            NativePumpModel::submit(
                &submitting_state,
                SubmissionOutcome::Submitted,
                NativeOutcome::Succeeded,
            )
        });

        state
            .cancel()
            .expect("event-loop cancellation must be modelled");
        let worker = submitter
            .join()
            .expect("submission actor must finish")
            .expect("submission actor must preserve lifecycle state");
        let expected_native_writer_closes = usize::from(worker.is_some());
        if let Some(worker) = worker {
            worker
                .join()
                .expect("worker actor must finish")
                .expect("worker actor must preserve lifecycle state");
        }
        state
            .observe_completion()
            .expect("observer actor must preserve lifecycle state");
        let snapshot = state
            .snapshot()
            .expect("snapshot must observe the settled lifecycle");
        assert_safe_terminal(snapshot, expected_native_writer_closes);
    });
}
