Feature: Telemetry adapters for structured execution events
  Cuprum provides example adapters that integrate execution events with common
  telemetry backends. These adapters remain optional and non-blocking.

  Background:
    Given a curated Python command for testing

  Scenario: Structured logging hook emits JSON-compatible records
    Given a structured logging hook with a test logger
    When I run a command that writes to stdout and stderr
    Then the logger receives records for all execution phases
    And each record contains cuprum-prefixed extra fields

  Scenario: Metrics hook collects execution counters and histograms
    Given an in-memory metrics collector
    When I run a command that succeeds
    Then the execution counter is incremented
    And the duration histogram contains an observation

  Scenario: Metrics hook tracks failure counts
    Given an in-memory metrics collector
    When I run a command that fails with non-zero exit
    Then the failure counter is incremented

  Scenario: Tracing hook creates spans with attributes
    Given an in-memory tracer
    When I run a command that writes output
    Then a span is created and ended
    And the span has program and exit code attributes
    And the span records output as events

  Scenario: Tracing hook sets error status on failure
    Given an in-memory tracer
    When I run a command that fails with non-zero exit
    Then the span status indicates an error
