Feature: Stream backend parity for edge cases
  Both the Python and Rust stream backends must produce identical
  pipeline output for edge cases including empty streams, multi-byte
  UTF-8, broken pipes, and backpressure.

  Scenario: Empty stream produces identical output across backends
    Given an empty stream pipeline
    When I run the parity pipeline synchronously
    Then the stdout is empty
    And the pipeline succeeded

  Scenario: Multi-byte UTF-8 survives pipeline across backends
    Given a pipeline producing multi-byte UTF-8 data
    When I run the parity pipeline synchronously
    Then the stdout matches the expected UTF-8 payload
    And the pipeline succeeded

  Scenario: Broken pipe is handled gracefully across backends
    Given a pipeline with an early-exiting downstream
    When I run the parity pipeline synchronously
    Then the pipeline completed without hanging

  Scenario: Large payload survives backpressure across backends
    Given a three stage pipeline with a one megabyte payload
    When I run the parity pipeline synchronously
    Then the stdout matches the expected large payload
    And the pipeline succeeded
