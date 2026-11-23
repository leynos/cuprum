//! Pure parsers behind the scenario parity guard.
//!
//! The feature grammar accepted here is deliberately narrower than Gherkin:
//! `Feature:`, plain `Scenario:`, steps, comments and blank lines. Anything
//! else fails closed with a line number, because an outline, table or doc
//! string would change what a binding executes and the counts the parity
//! comparison relies on would stop meaning what they claim. The comparison
//! itself lives in the `parity` child module; this module only reads text.

/// A scenario declared in the feature file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Declared {
    /// The declaration's title, with surrounding whitespace removed.
    pub(super) title: String,
    /// One-based line number of the declaration.
    pub(super) line: usize,
}

/// A `#[scenario(...)]` binding found in the module source.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Bound {
    /// The scenario title the attribute names.
    pub(super) name: String,
    /// The function the attribute is attached to.
    pub(super) function: String,
    /// One-based line number of the attribute.
    pub(super) line: usize,
}

/// The step keywords this grammar accepts.
const STEP_KEYWORDS: [&str; 5] = ["Given", "When", "Then", "And", "But"];

/// Headers whose behaviour this guard cannot count, named for the failure.
const UNSUPPORTED_HEADERS: [&str; 6] = [
    "Scenario Outline",
    "Scenario Template",
    "Examples",
    "Rule",
    "Background",
    "Scenario",
];

/// Parse the feature file's scenario declarations.
pub(super) fn declared_scenarios(feature_text: &str) -> Result<Vec<Declared>, String> {
    let mut declared = Vec::new();
    let mut saw_feature = false;

    for (index, raw_line) in feature_text.lines().enumerate() {
        let line_number = index.saturating_add(1);
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }

        if let Some(rest) = line.strip_prefix("Feature:") {
            if saw_feature {
                return Err(format!(
                    "line {line_number}: a feature file declares exactly one Feature header"
                ));
            }
            if rest.trim().is_empty() {
                return Err(format!(
                    "line {line_number}: the Feature header needs a title"
                ));
            }
            saw_feature = true;
            continue;
        }

        if let Some(rest) = line.strip_prefix("Scenario:") {
            if !saw_feature {
                return Err(format!(
                    "line {line_number}: a Scenario must follow the Feature header"
                ));
            }
            declared.push(declared_scenario(rest, line_number)?);
            continue;
        }

        if step_keyword(line).is_some() {
            if declared.is_empty() {
                return Err(format!(
                    "line {line_number}: a step must belong to a Scenario"
                ));
            }
            continue;
        }

        return Err(reject_line(line, line_number));
    }

    if !saw_feature {
        return Err("the feature file declares no Feature header".to_owned());
    }
    if declared.is_empty() {
        return Err("the feature file declares no scenarios".to_owned());
    }
    Ok(declared)
}

/// Build one declaration from the text after `Scenario:`.
fn declared_scenario(rest: &str, line_number: usize) -> Result<Declared, String> {
    let title = rest.trim();
    if title.is_empty() {
        return Err(format!(
            "line {line_number}: the Scenario declaration needs a title"
        ));
    }
    Ok(Declared {
        title: title.to_owned(),
        line: line_number,
    })
}

/// Name the grammar rule that rejects a line no other branch accepted.
fn reject_line(line: &str, line_number: usize) -> String {
    if line.starts_with('|') {
        return format!("line {line_number}: tables are not supported by this grammar");
    }
    if line.starts_with("\"\"\"") || line.starts_with("```") {
        return format!("line {line_number}: doc strings are not supported by this grammar");
    }
    if starts_with_localized_keyword(line) {
        return format!(
            "line {line_number}: localized keywords are not supported by this grammar, found \
             {line:?}"
        );
    }
    if declares_unsupported_header(line) {
        return format!(
            "line {line_number}: only plain Scenario declarations are supported, found {line:?}"
        );
    }
    format!(
        "line {line_number}: unsupported feature line {line:?}; this grammar accepts a Feature \
         header, plain Scenario declarations, steps, comments and blank lines"
    )
}

/// Match a line against the step vocabulary, returning its keyword.
fn step_keyword(line: &str) -> Option<&'static str> {
    STEP_KEYWORDS.into_iter().find(|keyword| {
        line.strip_prefix(keyword)
            .is_some_and(|rest| rest.starts_with(' '))
    })
}

/// Whether a line begins with a non-ASCII keyword such as `Scénario:`.
fn starts_with_localized_keyword(line: &str) -> bool {
    line.split_whitespace()
        .next()
        .is_some_and(|word| !word.is_ascii())
}

/// Whether a line looks like a header this grammar refuses to interpret.
fn declares_unsupported_header(line: &str) -> bool {
    let Some((candidate, _)) = line.split_once(':') else {
        return false;
    };
    let header = candidate.trim();
    UNSUPPORTED_HEADERS.contains(&header)
        || header
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_uppercase())
}

/// Parse every `#[scenario(...)]` binding in the module source.
///
/// The source is rustfmt's output, so an attribute runs from the line that
/// opens `#[scenario(` to the first line that closes it with `)]`. Each
/// attribute must name its scenario, must name the expected feature path, and
/// must be followed by the `fn` item it decorates.
pub(super) fn bound_scenarios(source: &str, expected_path: &str) -> Result<Vec<Bound>, String> {
    let lines: Vec<&str> = source.lines().collect();
    let mut bound = Vec::new();
    let mut index = 0;

    while let Some(line) = lines.get(index) {
        if !line.trim().starts_with("#[scenario(") {
            index = index.saturating_add(1);
            continue;
        }

        let start_line = index.saturating_add(1);
        let mut attribute = String::new();
        let mut closed = false;
        while let Some(current) = lines.get(index) {
            attribute.push_str(current.trim());
            index = index.saturating_add(1);
            if attribute.ends_with(")]") {
                closed = true;
                break;
            }
        }
        if !closed {
            return Err(format!(
                "line {start_line}: the scenario attribute is never closed"
            ));
        }

        let path = quoted_value(&attribute, "path")
            .map_err(|reason| format!("line {start_line}: {reason}"))?;
        if path != expected_path {
            return Err(format!(
                "line {start_line}: the scenario binding must name {expected_path:?}, found \
                 {path:?}"
            ));
        }
        if names_index(&attribute) {
            return Err(format!(
                "line {start_line}: bind every scenario by name; an index binding silently \
                 selects a declaration by position"
            ));
        }
        let name = quoted_value(&attribute, "name")
            .map_err(|reason| format!("line {start_line}: {reason}"))?;
        let function = next_function_name(&lines, index)
            .map_err(|reason| format!("line {start_line}: {reason}"))?;

        bound.push(Bound {
            name,
            function,
            line: start_line,
        });
    }

    Ok(bound)
}

/// Extract the quoted value of `key = "..."` from an attribute body.
fn quoted_value(attribute: &str, key: &str) -> Result<String, String> {
    let needle = format!("{key} = \"");
    let start = attribute
        .find(&needle)
        .ok_or_else(|| format!("the scenario attribute must name `{key}`"))?;
    let rest = attribute
        .get(start.saturating_add(needle.len())..)
        .ok_or_else(|| format!("the `{key}` value is malformed"))?;
    let end = rest
        .find('"')
        .ok_or_else(|| format!("the `{key}` value is unterminated"))?;
    rest.get(..end)
        .map(str::to_owned)
        .ok_or_else(|| format!("the `{key}` value is malformed"))
}

/// Whether the attribute binds by `index` rather than by name.
fn names_index(attribute: &str) -> bool {
    let mut rest = attribute;
    while let Some(position) = rest.find("index") {
        rest = rest
            .get(position.saturating_add("index".len())..)
            .unwrap_or("");
        if rest.trim_start().starts_with('=') {
            return true;
        }
    }
    false
}

/// Return the identifier of the first `fn` item after `index`.
fn next_function_name(lines: &[&str], mut index: usize) -> Result<String, String> {
    while let Some(line) = lines.get(index).map(|line| line.trim()) {
        index = index.saturating_add(1);
        if line.is_empty() || line.starts_with('#') || line.starts_with("//") {
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

mod parity;

pub(crate) use parity::{ParityTable, check_parity};

#[cfg(test)]
mod tests;
