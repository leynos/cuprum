Feature: Structured execution events
  Cuprum emits structured execution events that can be consumed by telemetry
  integrations.

  Scenario: Observe hook receives output events and timing metadata
    Given a safe command that writes to stdout and stderr
    When I run the command with an observe hook
    Then the observe hook sees stdout and stderr line events
    And the observe hook sees timing and tag metadata

