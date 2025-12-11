Feature: Logging hook
  Provides structured start and exit logging via the context hooks.

  Scenario: Logging hook emits start and exit events
    Given a logging hook registered for echo
    When I run a safe echo command
    Then start and exit events are logged
