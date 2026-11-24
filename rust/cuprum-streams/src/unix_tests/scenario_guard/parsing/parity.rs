//! Multiset comparison of the feature, the bindings, and the table.
//!
//! This is the half of the guard that answers "does anything disagree?" It
//! indexes each side by key and reports every discrepancy in one pass, so a
//! single run names all of them rather than stopping at the first.

use std::collections::BTreeMap;

use super::{Bound, Declared};

/// The expectation table, projected into the two views parity compares.
///
/// The table rows pair a title with its binding, but parity needs each column
/// on its own. Building both here keeps `check_parity` at three parameters and
/// keeps the two projections from drifting apart.
pub(crate) struct ParityTable<'a> {
    /// The table's `(title, binding function)` rows, in declaration order.
    ///
    /// The rows are kept together rather than projected into two columns: a
    /// column pair cannot say which function belongs to which title, so a swap
    /// between two rows would compare equal on both columns.
    rows: Vec<(&'a str, &'a str)>,
}

impl<'a> ParityTable<'a> {
    /// Retain the table's rows as declared.
    pub(crate) fn from_rows(rows: &[(&'a str, &'a str)]) -> Self {
        Self {
            rows: rows.to_vec(),
        }
    }

    /// The title column, in declaration order.
    fn titles(&self) -> impl Iterator<Item = &'a str> + '_ {
        self.rows.iter().map(|(title, _)| *title)
    }

    /// The binding-function column, in declaration order.
    fn bindings(&self) -> impl Iterator<Item = &'a str> + '_ {
        self.rows.iter().map(|(_, binding)| *binding)
    }
}

/// The five key indexes a parity comparison reads.
struct Indexes<'a> {
    declared_titles: BTreeMap<&'a str, Vec<usize>>,
    bound_names: BTreeMap<&'a str, Vec<usize>>,
    bound_functions: BTreeMap<&'a str, Vec<usize>>,
    expected_titles: BTreeMap<&'a str, Vec<usize>>,
    expected_bindings: BTreeMap<&'a str, Vec<usize>>,
}

impl<'a> Indexes<'a> {
    /// Project each side of the comparison into its key index.
    fn build(declared: &'a [Declared], bound: &'a [Bound], table: &'a ParityTable<'a>) -> Self {
        Self {
            declared_titles: index_by(
                declared
                    .iter()
                    .map(|entry| (entry.title.as_str(), entry.line)),
            ),
            bound_names: index_by(bound.iter().map(|entry| (entry.name.as_str(), entry.line))),
            bound_functions: index_by(
                bound
                    .iter()
                    .map(|entry| (entry.function.as_str(), entry.line)),
            ),
            expected_titles: index_by(
                table
                    .titles()
                    .enumerate()
                    .map(|(position, title)| (title, position.saturating_add(1))),
            ),
            expected_bindings: index_by(
                table
                    .bindings()
                    .enumerate()
                    .map(|(position, binding)| (binding, position.saturating_add(1))),
            ),
        }
    }
}

/// Compare the declarations, bindings, and expectation table as multisets.
///
/// Every table title must be declared exactly once, named by exactly one
/// binding, and backed by exactly one binding function; nothing may appear on
/// one side alone. All problems are reported together so one run names every
/// discrepancy.
pub(crate) fn check_parity(
    declared: &[Declared],
    bound: &[Bound],
    table: &ParityTable<'_>,
) -> Result<(), String> {
    let indexes = Indexes::build(declared, bound, table);
    let mut problems = Vec::new();
    report_duplicates(
        &mut problems,
        "feature declaration",
        &indexes.declared_titles,
    );
    report_duplicates(&mut problems, "scenario binding name", &indexes.bound_names);
    report_duplicates(
        &mut problems,
        "scenario binding function",
        &indexes.bound_functions,
    );
    report_duplicates(
        &mut problems,
        "expectation table title",
        &indexes.expected_titles,
    );
    report_duplicates(
        &mut problems,
        "expectation table binding",
        &indexes.expected_bindings,
    );

    // The title and function columns are also compared column-wise, but that
    // alone would accept a swap: rows (A, first) and (B, second) agree with
    // bindings (B, first) and (A, second) on both columns independently. The
    // pair comparison below is what pins each title to its own function.
    let comparisons = [
        (
            ("feature declaration", &indexes.declared_titles),
            ("expectation table title", &indexes.expected_titles),
        ),
        (
            ("scenario binding name", &indexes.bound_names),
            ("expectation table title", &indexes.expected_titles),
        ),
        (
            ("scenario binding function", &indexes.bound_functions),
            ("expectation table binding", &indexes.expected_bindings),
        ),
    ];
    for ((left_label, left_index), (right_label, right_index)) in comparisons {
        report_unmatched(
            &mut problems,
            (
                &Side {
                    label: left_label,
                    index: left_index,
                },
                &Side {
                    label: right_label,
                    index: right_index,
                },
            ),
        );
    }

    report_mispaired(&mut problems, bound, table);

    if problems.is_empty() {
        Ok(())
    } else {
        Err(problems.join("; "))
    }
}

/// Report a title whose own row expects a different binding function.
///
/// Column-wise comparison cannot see this: it answers "does every expected
/// function appear somewhere?", not "does each title's row name the function
/// that actually implements it?".
fn report_mispaired(problems: &mut Vec<String>, bound: &[Bound], table: &ParityTable<'_>) {
    let by_name: BTreeMap<&str, &str> = bound
        .iter()
        .map(|entry| (entry.name.as_str(), entry.function.as_str()))
        .collect();

    for (title, expected_function) in &table.rows {
        let Some(actual_function) = by_name.get(title) else {
            // A missing or duplicated title is already reported by the
            // column comparisons; naming it twice would bury the real cause.
            continue;
        };
        if *actual_function != *expected_function {
            problems.push(format!(
                "expectation table row {title:?} expects binding {expected_function:?}, but the \
                 binding named {title:?} calls {actual_function:?}",
            ));
        }
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

/// One side of a parity comparison: its label and its key index.
struct Side<'a> {
    label: &'a str,
    index: &'a BTreeMap<&'a str, Vec<usize>>,
}

/// Report keys that appear on only one of the two sides.
fn report_unmatched(problems: &mut Vec<String>, sides: (&Side<'_>, &Side<'_>)) {
    let (left, right) = sides;
    for (key, positions) in left.index {
        if !right.index.contains_key(key) {
            problems.push(format!(
                "{} {key:?} (at {positions:?}) has no matching {}",
                left.label, right.label,
            ));
        }
    }
    for key in right.index.keys() {
        if !left.index.contains_key(key) {
            problems.push(format!(
                "{} {key:?} has no matching {}",
                right.label, left.label,
            ));
        }
    }
}
