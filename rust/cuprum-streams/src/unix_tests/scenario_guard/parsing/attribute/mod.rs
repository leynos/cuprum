//! Top-level argument parsing for a `#[scenario(...)]` attribute.
//!
//! Searching the attribute's raw text for `key = "` is not enough: a key name
//! can appear inside another argument's string value, and a string value can
//! contain an escaped quote. Both mistakes silently truncate or misread a
//! binding, so this module splits the argument list properly — on commas
//! outside strings and brackets — and decodes each Rust string literal.
//!
//! Decoding is deliberately narrow. This guard only has to compare titles it
//! can reproduce exactly, so an escape outside the common set is refused
//! rather than guessed at: a wrong verdict from a half-understood literal is
//! worse than a failure naming the escape.

/// Which `#[scenario(...)]` argument a diagnostic is about.
///
/// Naming the two keys replaces a `&str` parameter on every helper that
/// builds a diagnostic, so the key vocabulary lives in one place.
#[derive(Clone, Copy, Debug)]
pub(super) enum ArgKey {
    /// The feature file the binding must name.
    Path,
    /// The scenario title the binding must name.
    Name,
}

impl ArgKey {
    /// The key's spelling in the attribute source.
    pub(super) const fn as_str(self) -> &'static str {
        match self {
            Self::Path => "path",
            Self::Name => "name",
        }
    }

    /// Recover the key from its spelling, refusing anything else.
    fn parse(spelling: &str) -> Option<Self> {
        match spelling {
            "path" => Some(Self::Path),
            "name" => Some(Self::Name),
            _ => None,
        }
    }
}

/// One top-level argument of a `#[scenario(...)]` attribute.
#[derive(Debug)]
pub(super) struct AttributeArg {
    /// Which argument this is.
    key: ArgKey,
    /// The argument's value.
    value: ArgValue,
}

/// The value side of an attribute argument.
#[derive(Debug)]
enum ArgValue {
    /// A Rust string literal, with its escapes decoded.
    Text(String),
    /// Any other expression, such as an integer index.
    Bare,
}

impl AttributeArg {
    /// Return the decoded string value when this argument is `key`.
    pub(super) fn text(&self, key: ArgKey) -> Option<&str> {
        match &self.value {
            ArgValue::Text(text) if self.key.as_str() == key.as_str() => Some(text),
            ArgValue::Text(_) | ArgValue::Bare => None,
        }
    }
}

/// Parse the top-level arguments of an attribute body.
///
/// The body must be `#[scenario(...)]`-shaped: text from the opening `(` to
/// the closing `)]`. Nested brackets inside a value are preserved rather than
/// split on, so a future `path = concat!("a", "b")` stays one argument.
pub(super) fn attribute_args(body: &str) -> Result<Vec<AttributeArg>, String> {
    let open = body
        .find('(')
        .ok_or_else(|| "the scenario attribute has no argument list".to_owned())?;
    let close = body
        .rfind(')')
        .ok_or_else(|| "the scenario attribute argument list is never closed".to_owned())?;
    let inner = body
        .get(open.saturating_add(1)..close)
        .ok_or_else(|| "the scenario attribute argument list is malformed".to_owned())?;

    let mut args = Vec::new();
    for entry in split_top_level(inner) {
        let trimmed = entry.trim();
        if trimmed.is_empty() {
            continue;
        }
        args.push(parse_arg(trimmed)?);
    }
    Ok(args)
}

/// Split an argument list on the commas that separate its top-level entries.
fn split_top_level(inner: &str) -> Vec<String> {
    let mut scanner = ArgScanner::default();
    for character in inner.chars() {
        scanner.observe(character);
    }
    scanner.finish()
}

/// Where the scanner is within one character of the argument list.
#[derive(Clone, Copy, Default, PartialEq, Eq)]
enum ScannerPhase {
    /// Outside any string literal.
    #[default]
    Between,
    /// Inside a string literal, with no escape pending.
    InString,
    /// Immediately after a backslash inside a string literal.
    Escaping,
}

/// Splits an argument list, tracking string literals and bracket depth.
///
/// One character at a time, so the state that matters — whether a comma is a
/// separator or literal text — is named rather than inferred from nesting.
#[derive(Default)]
struct ArgScanner {
    pieces: Vec<String>,
    current: String,
    depth: usize,
    phase: ScannerPhase,
}

impl ArgScanner {
    /// Consume one character of the argument list.
    fn observe(&mut self, character: char) {
        match self.phase {
            ScannerPhase::Escaping => self.end_escape(character),
            ScannerPhase::InString => self.in_string(character),
            ScannerPhase::Between => self.between(character),
        }
    }

    /// Record a character inside a string literal.
    fn in_string(&mut self, character: char) {
        self.current.push(character);
        match character {
            '\\' => self.phase = ScannerPhase::Escaping,
            '"' => self.phase = ScannerPhase::Between,
            _ => {}
        }
    }

    /// Record the character an escape applies to.
    fn end_escape(&mut self, character: char) {
        self.current.push(character);
        self.phase = ScannerPhase::InString;
    }

    /// Record a character outside any string literal.
    fn between(&mut self, character: char) {
        match character {
            '"' => {
                self.phase = ScannerPhase::InString;
                self.current.push(character);
            }
            '(' | '[' | '{' => {
                self.depth = self.depth.saturating_add(1);
                self.current.push(character);
            }
            ')' | ']' | '}' => {
                self.depth = self.depth.saturating_sub(1);
                self.current.push(character);
            }
            ',' if self.depth == 0 => self.pieces.push(std::mem::take(&mut self.current)),
            _ => self.current.push(character),
        }
    }

    /// Return every piece, including the trailing one after the last comma.
    fn finish(self) -> Vec<String> {
        let mut pieces = self.pieces;
        pieces.push(self.current);
        pieces
    }
}

/// Split one `key = value` entry and decode a string value when it is one.
fn parse_arg(entry: &str) -> Result<AttributeArg, String> {
    let (raw_key, raw_value) = entry
        .split_once('=')
        .ok_or_else(|| format!("the scenario attribute argument {entry:?} has no value"))?;
    let key = ArgKey::parse(raw_key.trim())
        .ok_or_else(|| format!("the scenario attribute has an unknown argument {raw_key:?}"))?;
    let value = match raw_value.trim().strip_prefix('"') {
        Some(rest) => ArgValue::Text(decode_rust_string(rest, key)?),
        None => ArgValue::Bare,
    };
    Ok(AttributeArg { key, value })
}

/// Decode a Rust string literal body, which starts just after the open quote.
fn decode_rust_string(body: &str, key: ArgKey) -> Result<String, String> {
    let mut decoded = String::new();
    let mut chars = body.chars();

    while let Some(character) = chars.next() {
        if character == '"' {
            return Ok(decoded);
        }
        if character != '\\' {
            decoded.push(character);
            continue;
        }
        let escape = chars
            .next()
            .ok_or_else(|| format!("the `{}` value ends inside an escape", key.as_str()))?;
        decoded.push(decode_escape(escape, &mut chars, key)?);
    }

    Err(format!("the `{}` value is unterminated", key.as_str()))
}

/// Translate one escape sequence, leaving its introducer already consumed.
fn decode_escape(
    escape: char,
    chars: &mut std::str::Chars<'_>,
    key: ArgKey,
) -> Result<char, String> {
    match escape {
        '"' => Ok('"'),
        '\'' => Ok('\''),
        '\\' => Ok('\\'),
        'n' => Ok('\n'),
        'r' => Ok('\r'),
        't' => Ok('\t'),
        '0' => Ok('\0'),
        'u' => decode_unicode_escape(chars, key),
        other => Err(format!(
            "the `{}` value uses the unsupported escape `\\{other}`; extend the decoder rather \
             than guessing its meaning",
            key.as_str(),
        )),
    }
}

/// Decode a `\u{...}` escape, whose introducer has already been consumed.
fn decode_unicode_escape(chars: &mut std::str::Chars<'_>, key: ArgKey) -> Result<char, String> {
    if chars.next() != Some('{') {
        return Err(format!(
            "the `{}` value has a malformed `\\u` escape",
            key.as_str()
        ));
    }
    let mut digits = String::new();
    for character in chars.by_ref() {
        if character == '}' {
            break;
        }
        digits.push(character);
    }
    let value = u32::from_str_radix(&digits, 16).map_err(|error| {
        format!(
            "the `{}` value has a malformed `\\u{{{digits}}}`: {error}",
            key.as_str()
        )
    })?;
    char::from_u32(value).ok_or_else(|| {
        format!(
            "the `{}` value has a `\\u{{{digits}}}` that is not a character",
            key.as_str()
        )
    })
}

#[cfg(test)]
mod tests;
