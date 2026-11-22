Feature: Optional Rust stream operations
  Cuprum ships a Rust extension with stream operations that transfer bytes
  between file descriptors or decode output for high-throughput pipelines.

  Scenario: Rust pump stream transfers data between pipes
    Given the Rust pump stream is available
    When I pump a payload through the Rust stream
    Then the output matches the payload

  Scenario: Rust consume stream replaces invalid UTF-8
    Given the Rust consume stream is available
    When I consume a payload with invalid UTF-8
    Then the decoded output matches replacement semantics

  Scenario: A large malformed stream preserves Python replacement decoding
    Given a payload of 1048577 bytes ending with an incomplete UTF-8 sequence
    When the native and Python consumers capture the complete payload
    Then both captures equal Python UTF-8 replacement decoding
    And omitted and explicit default buffers produce the same capture
    And the caller's reader descriptor remains open

  Scenario: Rust pump stream handles large pipe transfers
    Given the Rust pump stream is available
    When I pump a large payload through the Rust stream
    Then the output matches the large payload

  Scenario Outline: Preserve native stream exception categories
    Given the compiled Rust backend is required
    When the <operation> native helper receives <failure>
    Then it raises <exception>

    Examples:
      | operation | failure              | exception  |
      | pump      | a zero buffer size   | ValueError |
      | consume   | a zero buffer size   | ValueError |
      | pump      | a fatal reader error | OSError    |
      | consume   | a fatal reader error | OSError    |

  Scenario Outline: The buffer size is the first reported stream argument failure
    Given the stream adapter entry points are available
    When the <operation> entry point receives descriptor <descriptor> and buffer size <buffer_size>
    Then it raises <exception> saying "<message>"

    Examples:
      | operation | descriptor | buffer_size | exception  | message                               |
      | pump      | -1         | 0           | ValueError | buffer_size must be greater than zero |
      | consume   | -1         | 0           | ValueError | buffer_size must be greater than zero |
      | pump      | 2147483648 | 0           | ValueError | buffer_size must be greater than zero |
      | consume   | 2147483648 | 0           | ValueError | buffer_size must be greater than zero |

  Scenario Outline: Both adapters apply the same raw descriptor policy
    Given the stream adapter entry points are available
    When the <operation> entry point receives descriptor <descriptor> and buffer size <buffer_size>
    Then it raises <exception> saying "<message>"

    Examples:
      | operation | descriptor          | buffer_size | exception     | message                                   |
      | pump      | -1                  | 65536       | ValueError    | file descriptor must be non-negative      |
      | consume   | -1                  | 65536       | ValueError    | file descriptor must be non-negative      |
      | pump      | 2147483648          | 65536       | ValueError    | file descriptor out of range              |
      | consume   | 2147483648          | 65536       | ValueError    | file descriptor out of range              |
      | pump      | 9223372036854775808 | 65536       | OverflowError | Python int too large to convert to C long |
      | consume   | 9223372036854775808 | 65536       | OverflowError | Python int too large to convert to C long |

  Scenario Outline: Non-index-like stream arguments raise TypeError
    Given the stream adapter entry points are available
    When the <operation> entry point receives a non-index-like <argument>
    Then it raises TypeError leaving the caller's reader unread

    Examples:
      | operation | argument |
      | pump      | reader   |
      | pump      | buffer   |
      | consume   | reader   |
      | consume   | buffer   |

  Scenario: A Windows writer transfer failure raises OSError
    Given the stream adapter entry points are available
    And the Windows platform adapter is in use
    When the pump receives a closed writer descriptor
    Then it raises OSError and the payload remains unread

  Scenario: A Windows reader setup failure raises OSError
    Given the stream adapter entry points are available
    And the Windows platform adapter is in use
    When consume receives a closed reader descriptor
    Then it raises OSError

  Scenario: Index-like stream arguments are accepted by the Rust pump
    Given the stream adapter entry points are available
    When I pump a payload with index-like stream arguments
    Then the pump returns its payload and converted every argument once

  Scenario: Index-like stream arguments are accepted by consume
    Given the stream adapter entry points are available
    When I consume a payload with index-like stream arguments
    Then consume returns its payload and converted every argument once
