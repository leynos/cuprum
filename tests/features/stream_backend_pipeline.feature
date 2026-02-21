Feature: Pipeline execution with stream backend selection
  Pipelines produce correct results regardless of whether the pure
  Python or Rust stream backend is used for inter-stage pumping.

  Scenario: Pipeline streams output using the Python backend
    Given the stream backend is set to python
    And a simple two stage uppercase pipeline
    When I run the pipeline synchronously
    Then the pipeline stdout is the uppercased input
    And all stages exit successfully

  Scenario: Pipeline streams output using the Rust backend
    Given the stream backend is set to rust
    And a simple two stage uppercase pipeline
    When I run the pipeline synchronously
    Then the pipeline stdout is the uppercased input
    And all stages exit successfully

  Scenario: Pipeline streams output using the auto backend
    Given the stream backend is set to auto
    And a simple two stage uppercase pipeline
    When I run the pipeline synchronously
    Then the pipeline stdout is the uppercased input
    And all stages exit successfully

  Scenario: Pipeline falls back to Python pumping when Rust cannot use FDs
    Given the stream backend is set to rust
    And the Rust extension is reported as available
    And inter-stage file descriptor extraction fails
    And a simple two stage uppercase pipeline
    When I run the pipeline synchronously
    Then the pipeline stdout is the uppercased input
    And all stages exit successfully
    And the Python pump fallback is used

  Scenario: Pipeline raises when Rust is forced but unavailable
    Given the stream backend is set to rust
    And the Rust extension is not available for pipeline execution
    And a pipeline with an immediately exiting downstream stage
    When I attempt to run the pipeline synchronously
    Then an ImportError is raised during pipeline execution
