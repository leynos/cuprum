Feature: Stream fidelity
  Cuprum preserves data integrity when streaming through pipelines.

  Scenario: Pipeline preserves 512 lines of random data
    Given 512 lines of deterministic random base64 data
    When I pipe the data through cat synchronously
    Then the output matches the snapshot
