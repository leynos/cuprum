Feature: Owned live byte streams

  A caller observes a running child's bytes without decoding them, while
  retaining the owned lifecycle that stops, reaps, and settles the child.

  Scenario: A stdout prefix releases a stdin producer
    Given a live byte command that waits for a token after a prefix
    When the caller observes the prefix and releases the token
    Then the live stdout bytes are the prefix followed by the token
    And the settled byte result carries the same bytes

  Scenario: stdout reaches its own end while stderr stays live
    Given a live byte command that closes stdout and then writes stderr
    When the caller consumes stdout to its end
    Then stdout reports EOF
    And stderr is still live and the child is still running
    When the caller consumes stderr to its end
    Then stderr reports EOF

  Scenario: A line observer is refused before a child exists
    Given a byte command configured with a line observer
    When the caller starts it as a live byte stream
    Then a ValueError naming on_line is raised