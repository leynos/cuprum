Feature: Typed native stream failures
  The native stream boundary classifies every failure before it becomes a
  Python exception, so a caller receives ValueError for an invalid argument
  and OSError for a fatal stream failure. These scenarios exercise that
  classification through the real Rust entry points, without an interpreter.

  Scenario: Reject an invalid buffer before stream preparation
    Given a buffer size of 0
    When the native buffer validator checks the size
    Then the error is InvalidBufferSize
    And its message is buffer_size must be greater than zero

  Scenario: Reject a buffer above the cap
    Given a buffer size of 1073741825
    When the native buffer validator checks the size
    Then the error is InvalidBufferSize
    And its message is buffer_size exceeds the maximum permitted size

  Scenario Outline: Accept a valid buffer size
    Given a buffer size of <size>
    When the native buffer validator checks the size
    Then the size is accepted as <size>

    Examples:
      | size       |
      | 1          |
      | 65536      |
      | 1073741824 |

  Scenario: Retain a native I/O failure
    Given a stream I/O error with platform error code 9
    When it becomes a RustStreamError
    Then the error is Stream
    And the stream error retains platform error code 9

  Scenario: Retain a semantic stream failure
    Given a semantic stream failure of BufferRangeExceeded
    When it becomes a RustStreamError
    Then the error is Stream
    And the stream error retains the semantic message computed range exceeded the buffer bounds
