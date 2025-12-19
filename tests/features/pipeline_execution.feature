Feature: Pipeline execution
  Pipelines connect safe commands via stdout/stdin streaming.

  Scenario: Pipeline streams output between stages
    Given a simple two stage pipeline
    When I run the pipeline synchronously
    Then the pipeline output is transformed
    And the pipeline exposes per stage exit metadata

  Scenario: Pipeline can run asynchronously
    Given a simple two stage pipeline
    When I run the pipeline asynchronously
    Then the pipeline output is transformed

  Scenario: Pipeline reports metadata when a stage fails
    Given a two stage pipeline with a failing first stage
    When I run the pipeline synchronously
    Then the pipeline exposes per stage exit metadata when a stage fails
