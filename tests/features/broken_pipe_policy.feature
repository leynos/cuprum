Feature: Opt-in broken-pipe policy for echoed output
  A presentation sink writes to a destination the caller does not own, and
  that destination can close before the child stops writing. By default the
  resulting BrokenPipeError propagates and the run yields no result; a caller
  who expects a downstream reader to come and go can opt into abandoning the
  echo for the affected stream while the rest of the run continues.

  Background:
    Given a curated Python command for testing

  Scenario: Best-effort echo returns the captured result after the sink closes
    Given a presentation sink whose destination has closed
    When I run an echoed command under the best-effort broken-pipe policy
    Then the run returns a result with its stdout captured
    And the result records one broken-pipe relay fallback on stdout

  Scenario: Strict echo still propagates the broken pipe
    Given a presentation sink whose destination has closed
    When I run an echoed command under the default policy
    Then the run raises BrokenPipeError instead of returning a result

  Scenario: Best-effort echo leaves capture, line observation and exit status intact
    Given a presentation sink whose destination has closed
    When I run an observed, echoed command under the best-effort broken-pipe policy
    Then the run returns a result with its stdout captured
    And the observed lines are complete despite the closed sink
    And the run reports the child's own exit status
