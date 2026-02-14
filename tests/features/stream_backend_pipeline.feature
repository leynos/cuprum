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
