Feature: Stream property preservation
  Cuprum preserves stream bytes across random payloads and chunk boundaries.

  Scenario Outline: Deterministic random payload preserves bytes across chunk boundaries
    Given a deterministic random payload with seed <seed> and size <size>
    When I run the stream property pipeline synchronously
    Then the stream property output matches the expected hex payload
    And the pipeline completes successfully

    Examples:
      | seed  | size |
      | 60217 | 257  |
      | 60218 | 4352 |
