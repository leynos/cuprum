# Behavioural coverage for the public Unix stream entry points.
#
# Every scenario drives `pump_stream` or `consume_stream` directly over real
# pipe descriptors, so the ownership contract those entry points carry — the
# writer is consumed and closed on every exit, the borrowed reader stays open —
# is exercised composed rather than through a crate-private seam.
#
# The grammar here is deliberately narrow: `scenario_guard` parses this file
# and fails closed on outlines, examples, rules, tables, doc strings and
# localized keywords, so each case stays a plain scenario with explicit steps.
# Payloads are named rather than spelled out; the raw bytes, including the
# invalid sequences, live beside the steps in `unix_tests/support.rs`.

Feature: Public Unix stream entry points

  # The buffer size exceeds the payload so the whole transfer occupies one
  # destination slot. A smaller chunk would need several slots while the sink
  # is still undrained, and a pipe that runs out of slots blocks its writer —
  # correct backpressure, but a deadlock in a scenario that reads the sink
  # only after the pump returns. Pipe ring depth is not a fixed number: the
  # kernel shrinks it under memory pressure.
  Scenario: Pump transfers a preloaded payload to the sink
    Given a source pipe preloaded with the transfer payload
    And a sink pipe
    When the public pump runs with buffer size 64
    Then the pump reports the payload length as transferred
    And the borrowed reader is still open
    And the pump closed its writer with the sink holding the payload

  Scenario: Consume decodes an empty stream
    Given a source pipe preloaded with the empty payload
    When the public consume runs with buffer size 1
    Then the consume output equals the decoded payload

  Scenario: Consume decodes a two-byte sequence split across reads
    Given a source pipe preloaded with the naive payload
    When the public consume runs with buffer size 1
    Then the consume output equals the decoded payload

  Scenario: Consume decodes a four-byte sequence split across reads
    Given a source pipe preloaded with the grinning payload
    When the public consume runs with buffer size 1
    Then the consume output equals the decoded payload

  Scenario: Consume replaces an invalid byte between reads
    Given a source pipe preloaded with the invalid-byte payload
    When the public consume runs with buffer size 1
    Then the consume output equals the decoded payload

  Scenario: Consume replaces a truncated trailing sequence
    Given a source pipe preloaded with the truncated-tail payload
    When the public consume runs with buffer size 1
    Then the consume output equals the decoded payload

  Scenario: Pump drains the reader after the destination hangs up
    Given a source pipe preloaded with the hangup payload
    And a sink pipe whose reader is closed
    When the public pump runs with buffer size 8
    Then the pump reports nothing transferred
    And the pump logged the broken-pipe drain
    And the source is drained to end of input

  Scenario: Pump reports a read failure from the source
    Given a source write end serving as the reader
    And a sink pipe
    When the public pump runs with buffer size 7
    Then the pump reports a stream I/O failure
    And the borrowed reader is still open
    And the pump closed its writer with the sink holding nothing

  Scenario: Pump closes its writer when the read unwinds
    Given a panicking reader over a live descriptor
    And a sink pipe
    When the public pump unwinds during its read
    Then the read unwinds exactly once
    And the borrowed reader is still open
    And the pump closed its writer with the sink holding nothing