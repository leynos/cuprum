Feature: Gating the paid benchmark job on changed paths
  benchmark-ratchet is the only job that runs on a paid runner, so a pull
  request pays for it only when its diff can plausibly change measured
  throughput. Pushes to main are never gated: that run republishes the
  baseline artefact every later comparison reads.

  Scenario: A documentation-only pull request skips the benchmark
    Given a pull request changing docs/users-guide.md and README.md
    When the workflow classifies the changed paths
    Then no performance-relevant change is detected
    And the benchmark job is skipped

  Scenario: A pull request touching the Rust extension benchmarks
    Given a pull request changing rust/cuprum-rust/src/pump.rs
    When the workflow classifies the changed paths
    Then a performance-relevant change is detected
    And the benchmark job runs

  Scenario: A dependency bump benchmarks even without a source change
    Given a pull request changing uv.lock
    When the workflow classifies the changed paths
    Then a performance-relevant change is detected
    And the benchmark job runs

  Scenario: A mixed pull request benchmarks
    Given a pull request changing cuprum/pipeline.py and docs/users-guide.md
    When the workflow classifies the changed paths
    Then a performance-relevant change is detected
    And the benchmark job runs

  Scenario: A push to main benchmarks whatever it touched
    Given a push to main changing docs/users-guide.md
    When the workflow classifies the changed paths
    Then no performance-relevant change is detected
    And the benchmark job runs

  Scenario: A pull request changing nothing skips the benchmark
    Given a pull request changing no files
    When the workflow classifies the changed paths
    Then no performance-relevant change is detected
    And the benchmark job is skipped
