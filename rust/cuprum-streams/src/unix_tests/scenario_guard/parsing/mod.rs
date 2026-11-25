//! Pure parsers behind the scenario parity guard.
//!
//! Two parsers live here, each reading one input language: the feature grammar
//! in [`feature`], and the `#[scenario]` attribute scanner in [`bindings`].
//! Both fail closed with a line number rather than guessing, so a construct
//! this guard cannot count is reported instead of being silently accepted.

mod attribute;
mod bindings;
mod feature;
mod parity;

pub(super) use bindings::bound_scenarios;
pub(super) use feature::declared_scenarios;
pub(crate) use parity::{ParityTable, check_parity};

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

#[cfg(test)]
mod tests;
