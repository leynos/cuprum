Feature: Benchmark CI Rust performance ratchet
  Maintainers can compare baseline and candidate throughput JSON outputs and
  fail CI when Rust benchmark performance regresses beyond threshold.

  Scenario: Ratchet passes when Rust regression stays within threshold
    Given benchmark comparison fixtures where candidate stays within threshold
    When I run the Rust benchmark ratchet CLI
    Then the ratchet command exits successfully
    And the ratchet report indicates success

  Scenario: Ratchet fails when Rust regression exceeds threshold
    Given benchmark comparison fixtures where candidate exceeds threshold
    When I run the Rust benchmark ratchet CLI
    Then the ratchet command exits with failure
    And the ratchet report indicates regression failure
