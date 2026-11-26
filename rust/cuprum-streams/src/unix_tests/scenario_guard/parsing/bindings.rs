//! The scanner for `#[scenario(...)]` attributes in the module source.
//!
//! This reads Rust source rather than feature text: it finds each attribute,
//! joins its lines, and resolves the `fn` item that follows it. Keeping it
//! separate from the feature grammar leaves each parser reading one language.

use super::{
    Bound,
    attribute::{ArgKey, AttributeArg, attribute_args},
};

/// The Rust source being scanned, split into lines once.
///
/// The scanner walks this repeatedly — for each attribute and again for the
/// `fn` item after it — so the lines are held rather than re-split, and the
/// type marks every helper's input as "the module source" rather than any
/// string that happens to be nearby.
struct Source<'a> {
    lines: Vec<&'a str>,
}

impl<'a> Source<'a> {
    /// Split `text` into the lines the scan walks.
    fn new(text: &'a str) -> Self {
        Self {
            lines: text.lines().collect(),
        }
    }

    /// The line at `index`, trimmed, or `None` past the end.
    fn trimmed(&self, index: usize) -> Option<&'a str> {
        self.lines.get(index).map(|line| line.trim())
    }
}

/// Parse every `#[scenario(...)]` binding in the module source.
///
/// The source is rustfmt's output, so an attribute runs from the line that
/// opens `#[scenario(` to the first line that closes it with `)]`. Each
/// attribute must bind by `name`, must name the expected feature path, and
/// must be followed by the `fn` item it decorates.
pub(crate) fn bound_scenarios(
    module_source: &str,
    expected_path: &str,
) -> Result<Vec<Bound>, String> {
    let source = Source::new(module_source);
    let mut bound = Vec::new();
    let mut cursor = 0;

    while let Some(found) = find_attribute(&source, cursor) {
        let AttributeText {
            text,
            start_line,
            next,
        } = found?;
        cursor = next;
        let args =
            attribute_args(&text).map_err(|reason| format!("line {start_line}: {reason}"))?;
        check_path(&args, expected_path, start_line)?;
        let name = argument_text(&args, ArgKey::Name)
            .map_err(|reason| format!("line {start_line}: {reason}"))?
            .to_owned();
        let function = next_function_name(&source, cursor)
            .map_err(|reason| format!("line {start_line}: {reason}"))?;
        bound.push(Bound {
            name,
            function,
            line: start_line,
        });
    }

    Ok(bound)
}

/// One collected `#[scenario(...)]` attribute and where the scan resumes.
struct AttributeText {
    /// The attribute's text, with line breaks removed.
    text: String,
    /// One-based line the attribute opens on.
    start_line: usize,
    /// Index of the first source line after the attribute.
    next: usize,
}

/// Find the next scenario attribute at or after `from`.
///
/// Returns the attribute, or `None` once no further attribute opens. Split out
/// of the scan so collecting one attribute is a single call rather than a loop
/// wrapping another loop.
fn find_attribute(source: &Source<'_>, from: usize) -> Option<Result<AttributeText, String>> {
    let mut index = from;
    while let Some(line) = source.trimmed(index) {
        if line.starts_with("#[scenario(") {
            return Some(collect_attribute(source, index));
        }
        index = index.saturating_add(1);
    }
    None
}

/// Collect the attribute that opens at `open`, joining its lines.
fn collect_attribute(source: &Source<'_>, open: usize) -> Result<AttributeText, String> {
    let start_line = open.saturating_add(1);
    let mut text = String::new();
    let mut index = open;

    while let Some(current) = source.trimmed(index) {
        text.push_str(current);
        index = index.saturating_add(1);
        if text.ends_with(")]") {
            return Ok(AttributeText {
                text,
                start_line,
                next: index,
            });
        }
    }
    Err(format!(
        "line {start_line}: the scenario attribute is never closed"
    ))
}

/// Require the attribute to name the expected feature file.
fn check_path(args: &[AttributeArg], expected_path: &str, start_line: usize) -> Result<(), String> {
    let path = argument_text(args, ArgKey::Path)
        .map_err(|reason| format!("line {start_line}: {reason}"))?;
    if path == expected_path {
        return Ok(());
    }
    Err(format!(
        "line {start_line}: the scenario binding must name {expected_path:?}, found {path:?}"
    ))
}

/// Read a required string argument from a parsed attribute.
fn argument_text(args: &[AttributeArg], key: ArgKey) -> Result<&str, String> {
    args.iter().find_map(|arg| arg.text(key)).ok_or_else(|| {
        format!(
            "the scenario attribute must name `{}` as a string literal",
            key.as_str()
        )
    })
}

/// Whether a source line cannot be the `fn` item an attribute decorates.
fn is_skippable_source_line(line: &str) -> bool {
    line.is_empty() || line.starts_with('#') || line.starts_with("//")
}

/// Return the identifier of the first `fn` item after `index`.
fn next_function_name(source: &Source<'_>, mut index: usize) -> Result<String, String> {
    while let Some(line) = source.trimmed(index) {
        index = index.saturating_add(1);
        if is_skippable_source_line(line) {
            continue;
        }
        let rest = line.strip_prefix("fn ").ok_or_else(|| {
            format!("expected the `fn` item the attribute decorates, found {line:?}")
        })?;
        let name: String = rest
            .chars()
            .take_while(|character| character.is_alphanumeric() || *character == '_')
            .collect();
        if name.is_empty() {
            return Err(format!("the decorated `fn` item has no name: {line:?}"));
        }
        return Ok(name);
    }
    Err("no `fn` item follows the scenario attribute".to_owned())
}
