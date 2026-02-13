Feature: Stream backend dispatcher
  Cuprum selects a stream backend at runtime based on the
  CUPRUM_STREAM_BACKEND environment variable and Rust extension
  availability. The dispatcher caches the availability check for
  performance.

  Scenario: Auto mode selects Python when Rust is unavailable
    Given the Rust extension is not available
    And the stream backend environment variable is unset
    When I resolve the stream backend
    Then the resolved backend is python

  Scenario: Forced Rust mode raises when extension is unavailable
    Given the Rust extension is not available
    And the stream backend is forced to rust
    When I attempt to resolve the stream backend
    Then an ImportError is raised

  Scenario: Forced Python mode always selects Python
    Given the stream backend is forced to python
    When I resolve the stream backend
    Then the resolved backend is python
