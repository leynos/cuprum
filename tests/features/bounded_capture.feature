Feature: Bounded capture policy on pipeline stages
  A bounded capture retains only the tail of a stream, so a noisy child cannot
  grow the parent's memory without bound. The bound governs what the run
  reports and nothing else: bytes a stage relays to the next stage are handed
  on as they arrive, so a bound on one stage must leave the stage after it
  reading the complete stream. Each stream carries its own policy, so a run may
  bound stdout while leaving stderr complete, or leave stderr out of the
  result entirely.

  Background:
    Given a curated Python command for testing

  Scenario: A bounded capture retains the tail of a stage's own output
    Given a producer stage whose payload exceeds the bound
    And a stage that relays its input to stdout and to stderr
    When I run the pipeline with stderr left uncaptured
    Then the reported stdout capture is the tail of the payload
    And the reported stdout capture counts the bytes it dropped
    And no stderr capture is reported

  Scenario: A bound never reaches the relay
    Given a producer stage whose payload exceeds the bound
    And a stage that reports how many bytes it read
    When I run the pipeline with stderr left uncaptured
    Then the downstream stage reports the payload's whole length

  Scenario: A bound on stdout leaves stderr complete, though both exceed it
    Given a producer stage whose payload exceeds the bound
    And a stage that relays its input to stdout and to stderr
    When I run the pipeline with stderr captured in full
    Then the reported stdout capture is the tail of the payload
    And the reported stderr capture is the whole stderr, which exceeds the bound

  Scenario: A bound never reaches the echo mirror
    Given a producer stage whose payload exceeds the bound
    And a stage that relays its input to stdout and to stderr
    When I run the pipeline with stdout bounded and echoed
    Then the echo mirror holds the whole payload
