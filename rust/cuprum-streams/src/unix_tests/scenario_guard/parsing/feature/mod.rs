//! The feature-file grammar: declarations, steps, and refusals.
//!
//! This parser reads Gherkin-shaped text and is deliberately narrower than
//! Gherkin: `Feature:`, plain `Scenario:`, steps, comments and blank lines.
//! Anything else fails closed with a line number, because an outline, table or
//! doc string would change what a binding executes and the counts the parity
//! comparison relies on would stop meaning what they claim.

use super::Declared;

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

/// One line of feature source, already trimmed.
///
/// This is the module's unit of grammar input. Carrying it as a newtype keeps
/// the parser's helpers from passing bare strings around, so a call either
/// supplies a line it read or the text after a keyword it matched — the two
/// are different things and now look different.
#[derive(Clone, Copy, Debug)]
struct Line<'a>(&'a str);

impl<'a> Line<'a> {
    /// The line's text.
    const fn text(self) -> &'a str { self.0 }

    /// The line with `keyword` removed when it starts with it.
    fn after(self, keyword: &str) -> Option<Self> { self.0.strip_prefix(keyword).map(Self) }

    /// Whether the line is empty or a comment.
    fn is_blank_or_comment(self) -> bool { self.0.is_empty() || self.0.starts_with('#') }

    /// Whether the line opens with `prefix`.
    fn starts_with(self, prefix: &str) -> bool { self.0.starts_with(prefix) }

    /// Whether the line opens with `character`.
    fn starts_with_char(self, character: char) -> bool { self.0.starts_with(character) }
}

/// Parse the feature file's scenario declarations.
pub(crate) fn declared_scenarios(feature_text: &str) -> Result<Vec<Declared>, String> {
    let mut state = DeclarationState::default();

    for (index, raw_line) in feature_text.lines().enumerate() {
        let line_number = index.saturating_add(1);
        state.accept(classify_feature_line(Line(raw_line.trim())), line_number)?;
    }

    state.finish()
}

/// What one feature line contributes to the declaration list.
#[derive(Clone, Copy)]
enum FeatureLine<'a> {
    /// A blank line or a comment.
    Ignorable,
    /// A `Feature:` header, carrying the text after the keyword.
    Feature(Line<'a>),
    /// A `Scenario:` declaration, carrying the text after the keyword.
    Scenario(Line<'a>),
    /// A step line.
    Step,
    /// A line this grammar refuses.
    Rejected(Line<'a>),
}

/// Decide a line's role from its leading keyword alone.
fn classify_feature_line(line: Line<'_>) -> FeatureLine<'_> {
    if line.is_blank_or_comment() {
        return FeatureLine::Ignorable;
    }
    if let Some(rest) = line.after("Feature:") {
        return FeatureLine::Feature(rest);
    }
    if let Some(rest) = line.after("Scenario:") {
        return FeatureLine::Scenario(rest);
    }
    if step_keyword(line).is_some() {
        return FeatureLine::Step;
    }
    FeatureLine::Rejected(line)
}

/// Declarations accepted so far, plus whether the Feature header was seen.
#[derive(Default)]
struct DeclarationState {
    declared: Vec<Declared>,
    saw_feature: bool,
    /// Line of the most recent declaration while it still has no steps.
    ///
    /// A scenario with no steps executes nothing, yet an `rstest-bdd` binding
    /// for it reports a passing test. The execution guard proves *that* a
    /// scenario ran, not what it did, so the grammar refuses a step-less
    /// declaration instead of letting the guard certify an empty run.
    awaiting_steps: Option<usize>,
}

impl DeclarationState {
    /// Apply one classified line, or explain why it is refused.
    fn accept(&mut self, line: FeatureLine<'_>, line_number: usize) -> Result<(), String> {
        match line {
            FeatureLine::Ignorable => Ok(()),
            FeatureLine::Feature(rest) => self.accept_feature(rest, line_number),
            FeatureLine::Scenario(rest) => self.accept_scenario(rest, line_number),
            FeatureLine::Step => self.accept_step(line_number),
            FeatureLine::Rejected(text) => Err(reject_line(text, line_number)),
        }
    }

    /// Validate a `Feature:` header, refusing a second one.
    fn accept_feature(&mut self, rest: Line<'_>, line_number: usize) -> Result<(), String> {
        if self.saw_feature {
            return Err(format!(
                "line {line_number}: a feature file declares exactly one Feature header"
            ));
        }
        if rest.text().trim().is_empty() {
            return Err(format!(
                "line {line_number}: the Feature header needs a title"
            ));
        }
        self.saw_feature = true;
        Ok(())
    }

    /// Record a declaration, refusing one that precedes the Feature header.
    fn accept_scenario(&mut self, rest: Line<'_>, line_number: usize) -> Result<(), String> {
        if !self.saw_feature {
            return Err(format!(
                "line {line_number}: a Scenario must follow the Feature header"
            ));
        }
        if let Some(step_less) = self.awaiting_steps {
            return Err(step_less_scenario(step_less));
        }
        self.declared.push(declared_scenario(rest, line_number)?);
        self.awaiting_steps = Some(line_number);
        Ok(())
    }

    /// Accept a step line only inside a scenario.
    fn accept_step(&mut self, line_number: usize) -> Result<(), String> {
        if self.declared.is_empty() {
            return Err(format!(
                "line {line_number}: a step must belong to a Scenario"
            ));
        }
        self.awaiting_steps = None;
        Ok(())
    }

    /// Require at least one Feature header and one declared scenario.
    fn finish(self) -> Result<Vec<Declared>, String> {
        if let Some(step_less) = self.awaiting_steps {
            return Err(step_less_scenario(step_less));
        }
        if !self.saw_feature {
            return Err("the feature file declares no Feature header".to_owned());
        }
        if self.declared.is_empty() {
            return Err("the feature file declares no scenarios".to_owned());
        }
        Ok(self.declared)
    }
}

/// Name the declaration that has no steps to run.
fn step_less_scenario(line_number: usize) -> String {
    format!(
        "line {line_number}: a Scenario needs at least one step; a scenario with none executes \
         nothing and would still report a passing test"
    )
}

/// Build one declaration from the text after `Scenario:`.
fn declared_scenario(rest: Line<'_>, line_number: usize) -> Result<Declared, String> {
    let title = rest.text().trim();
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
fn reject_line(line: Line<'_>, line_number: usize) -> String {
    if line.starts_with_char('|') {
        return format!("line {line_number}: tables are not supported by this grammar");
    }
    if line.starts_with("\"\"\"") || line.starts_with("```") {
        return format!("line {line_number}: doc strings are not supported by this grammar");
    }
    if starts_with_localized_keyword(line) {
        return format!(
            "line {line_number}: localized keywords are not supported by this grammar, found {:?}",
            line.text()
        );
    }
    if declares_unsupported_header(line) {
        return format!(
            "line {line_number}: only plain Scenario declarations are supported, found {:?}",
            line.text()
        );
    }
    format!(
        "line {line_number}: unsupported feature line {:?}; this grammar accepts a Feature \
         header, plain Scenario declarations, steps, comments and blank lines",
        line.text()
    )
}

/// Match a line against the step vocabulary, returning its keyword.
fn step_keyword(line: Line<'_>) -> Option<&'static str> {
    STEP_KEYWORDS.into_iter().find(|keyword| {
        line.after(keyword)
            .is_some_and(|rest| rest.starts_with(" "))
    })
}

/// Whether a line begins with a non-ASCII keyword such as `Scénario:`.
fn starts_with_localized_keyword(line: Line<'_>) -> bool {
    line.text()
        .split_whitespace()
        .next()
        .is_some_and(|word| !word.is_ascii())
}

/// Whether a line looks like a header this grammar refuses to interpret.
fn declares_unsupported_header(line: Line<'_>) -> bool {
    let Some((candidate, _)) = line.text().split_once(':') else {
        return false;
    };
    let header = candidate.trim();
    UNSUPPORTED_HEADERS.contains(&header)
        || header
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_uppercase())
}

#[cfg(test)]
mod tests;
