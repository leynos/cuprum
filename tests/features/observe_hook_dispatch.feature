Feature: Observe hook dispatch
  Every supported observe-hook shape receives every line event, and every
  awaitable it returns is awaited before the run returns.

  Scenario Outline: Every hook shape receives every line and settles before the run returns
    Given a safe command that prints 5 numbered lines to stdout
    And an observe hook shaped as <shape> that records stdout line events
    When the command runs synchronously with line events observed
    Then the hook records exactly 5 stdout line events in print order
    And no observe-hook task is pending when the run returns
    And no coroutine-never-awaited warning was emitted

    Examples:
      | shape                                  |
      | a synchronous function                 |
      | an async def function                  |
      | a lambda returning a coroutine         |
      | a functools.wraps wrapper of async def |
      | an instance with async __call__        |
