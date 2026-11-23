//! Pure parsers and comparisons behind the scenario parity guard.
//!
//! The feature grammar accepted here is deliberately narrower than Gherkin:
//! `Feature:`, plain `Scenario:`, steps, comments and blank lines. Anything
//! else fails closed with a line number, because an outline, table or doc
//! string would change what a binding executes and the counts this guard
//! compares would stop meaning what they claim.

use std::collections::BTreeMap;

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

        if let Some(title) = line.strip_prefix("Feature:") {
            if saw_feature {
                return Err(format!(
                    "line {line_number}: a feature file declares exactly one Feature header"
                ));
            }
            if title.trim().is_empty() {
                return Err(format!(
                    "line {line_number}: the Feature header needs a title"
                ));
            }
            saw_feature = true;
            continue;
        }

        if let Some(title) = line.strip_prefix("Scenario:") {
            if !saw_feature {
                return Err(format!(
                    "line {line_number}: a Scenario must follow the Feature header"
                ));
            }
            let title = title.trim();
            if title.is_empty() {
                return Err(format!(
                    "line {line_number}: the Scenario declaration needs a title"
                ));
            }
            declared.push(Declared {
                title: title.to_owned(),
                line: line_number,
            });
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

        if line.starts_with('|') {
            return Err(format!(
                "line {line_number}: tables are not supported by this grammar"
            ));
        }
        if line.starts_with("\"\"\"") || line.starts_with("```") {
            return Err(format!(
                "line {line_number}: doc strings are not supported by this grammar"
            ));
        }
        if starts_with_localized_keyword(line) {
            return Err(format!(
                "line {line_number}: localized keywords are not supported by this grammar, found \
                 {line:?}"
            ));
        }
        if declares_unsupported_header(line) {
            return Err(format!(
                "line {line_number}: only plain Scenario declarations are supported, found \
                 {line:?}"
            ));
        }
        return Err(format!(
            "line {line_number}: unsupported feature line {line:?}; this grammar accepts a \
             Feature header, plain Scenario declarations, steps, comments and blank lines"
        ));
    }

    if !saw_feature {
        return Err("the feature file declares no Feature header".to_owned());
    }
    if declared.is_empty() {
        return Err("the feature file declares no scenarios".to_owned());
    }
    Ok(declared)
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
    let Some((keyword, _)) = line.split_once(':') else {
        return false;
    };
    let keyword = keyword.trim();
    UNSUPPORTED_HEADERS.contains(&keyword)
        || keyword
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

/// Compare the declarations, bindings, and expectation table as multisets.
///
/// Every table title must be declared exactly once, named by exactly one
/// binding, and backed by exactly one binding function; nothing may appear on
/// one side alone. All problems are reported together so one run names every
/// discrepancy.
pub(super) fn check_parity(
    declared: &[Declared],
    bound: &[Bound],
    expected: &[(&str, &str)],
) -> Result<(), String> {
    let declared_titles = index_by(
        declared
            .iter()
            .map(|entry| (entry.title.as_str(), entry.line)),
    );
    let bound_names = index_by(bound.iter().map(|entry| (entry.name.as_str(), entry.line)));
    let bound_functions = index_by(
        bound
            .iter()
            .map(|entry| (entry.function.as_str(), entry.line)),
    );
    let expected_titles = index_by(
        expected
            .iter()
            .enumerate()
            .map(|(position, (title, _))| (*title, position.saturating_add(1))),
    );
    let expected_bindings = index_by(
        expected
            .iter()
            .enumerate()
            .map(|(position, (_, binding))| (*binding, position.saturating_add(1))),
    );

    let mut problems = Vec::new();
    report_duplicates(&mut problems, "feature declaration", &declared_titles);
    report_duplicates(&mut problems, "scenario binding name", &bound_names);
    report_duplicates(&mut problems, "scenario binding function", &bound_functions);
    report_duplicates(&mut problems, "expectation table title", &expected_titles);
    report_duplicates(
        &mut problems,
        "expectation table binding",
        &expected_bindings,
    );
    report_unmatched(
        &mut problems,
        "feature declaration",
        &declared_titles,
        "expectation table title",
        &expected_titles,
    );
    report_unmatched(
        &mut problems,
        "scenario binding name",
        &bound_names,
        "expectation table title",
        &expected_titles,
    );
    report_unmatched(
        &mut problems,
        "scenario binding function",
        &bound_functions,
        "expectation table binding",
        &expected_bindings,
    );

    if problems.is_empty() {
        Ok(())
    } else {
        Err(problems.join("; "))
    }
}

/// Index items by key, retaining every position the key appeared at.
fn index_by<'a>(items: impl Iterator<Item = (&'a str, usize)>) -> BTreeMap<&'a str, Vec<usize>> {
    let mut index: BTreeMap<&str, Vec<usize>> = BTreeMap::new();
    for (key, position) in items {
        index.entry(key).or_default().push(position);
    }
    index
}

/// Report keys that appear more than once in one source.
fn report_duplicates(problems: &mut Vec<String>, label: &str, index: &BTreeMap<&str, Vec<usize>>) {
    for (key, positions) in index {
        if positions.len() > 1 {
            problems.push(format!(
                "{label} {key:?} appears {} times at {positions:?}",
                positions.len()
            ));
        }
    }
}

/// Report keys that appear on only one of the two sides.
fn report_unmatched(
    problems: &mut Vec<String>,
    left_label: &str,
    left: &BTreeMap<&str, Vec<usize>>,
    right_label: &str,
    right: &BTreeMap<&str, Vec<usize>>,
) {
    for (key, positions) in left {
        if !right.contains_key(key) {
            problems.push(format!(
                "{left_label} {key:?} (at {positions:?}) has no matching {right_label}"
            ));
        }
    }
    for key in right.keys() {
        if !left.contains_key(key) {
            problems.push(format!(
                "{right_label} {key:?} has no matching {left_label}"
            ));
        }
    }
}

#[cfg(test)]
mod tests;
