Feature: Structured execution events
  Cuprum emits structured execution events that can be consumed by telemetry
  integrations.

  Scenario: Observe hook receives output events and timing metadata
    Given a safe command that writes to stdout and stderr
    When I run the command with an observe hook
    Then the observe hook sees stdout and stderr line events
    And the observe hook sees timing and tag metadata

  Scenario: Retained events preserve stream metadata and line order
    Given an observed command writes repeated and empty lines to both streams
    And the observer retains events until asynchronous callbacks settle
    When the command runs with captured and echoed output
    Then each stream has exactly the expected ordered line sequence
    And every line event retains its original line and timestamp
    And all line events carry the spawned process and resolved metadata
    And every stage has a plan with no process identifier and its own exit token

  Scenario: A pipeline preserves per-stream order and execution identity
    Given an observed two stage pipeline that writes to both streams
    And the observer retains events until asynchronous callbacks settle
    When the pipeline runs with captured and echoed output
    Then each stream has exactly the expected ordered line sequence
    And all line events carry the spawned process and resolved metadata
    And every stage has a plan with no process identifier and its own exit token

  Scenario: Concurrent runs of one command keep their events separate
    Given an observed command that echoes its own stdin
    And the observer retains events until asynchronous callbacks settle
    When the same command runs twice under distinct execution contexts
    Then each run's events carry that run's own execution token
    And each run sees only its own tagged lines

  Scenario: Unterminated and CRLF fragments survive line framing
    Given an observed command writes unterminated and CRLF output
    And the observer retains events until asynchronous callbacks settle
    When the command runs with captured and echoed output
    Then each stream has exactly the expected ordered line sequence
    And every line event retains its original line and timestamp
    And that command exits with the expected status

  Scenario: A silent command emits lifecycle events without line events
    Given an observed command that writes nothing to either stream
    And the observer retains events until asynchronous callbacks settle
    When the command runs with captured and echoed output
    Then the observe hook sees no line events for either stream
    And that command exits with the expected status

  Scenario: A failing command still reports its exit event
    Given an observed command that writes to both streams and exits non-zero
    And the observer retains events until asynchronous callbacks settle
    When the command runs with captured and echoed output
    Then each stream has exactly the expected ordered line sequence
    And that command exits with the expected status
