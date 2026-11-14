//! Parse the labelled coverage seeds used by the explicit proptest replay check.

/// Select the generator family associated with a curated coverage seed.
#[derive(Clone, Copy, Debug)]
pub(crate) enum SeedDomain {
    /// Replay the small arbitrary and UTF-8 category strategy.
    Small,
    /// Replay the shrinkable patterned large-payload strategy.
    Large,
}

/// Describe one project-owned deterministic coverage seed.
#[derive(Debug)]
pub(crate) struct CoverageSeed {
    /// Human-readable purpose recorded with replay output.
    pub(crate) label: String,
    /// Payload strategy that the fixed seed drives.
    pub(crate) domain: SeedDomain,
    /// Exact buffer used for the boundary replay.
    pub(crate) buffer_size: u32,
    /// Proptest 1.11.0 `RngSeed::Fixed` value.
    pub(crate) seed: u64,
}

/// Read labelled seeds without interpreting proptest regression-file syntax.
pub(crate) fn read_coverage_seeds() -> Result<Vec<CoverageSeed>, String> {
    include_str!("../fixtures/consume_boundary_seeds.txt")
        .lines()
        .enumerate()
        .filter_map(|(line_index, source)| {
            let line = source.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            Some(parse_coverage_seed(line_index + 1, line))
        })
        .collect()
}

fn parse_coverage_seed(line_number: usize, line: &str) -> Result<CoverageSeed, String> {
    let mut fields = line.split_whitespace();
    let [
        Some(label),
        Some(domain_name),
        Some(buffer_text),
        Some(seed_text),
    ] = [fields.next(), fields.next(), fields.next(), fields.next()]
    else {
        return Err(format!(
            "coverage seed line {line_number} must contain label, domain, buffer, and seed"
        ));
    };
    if fields.next().is_some() {
        return Err(format!(
            "coverage seed line {line_number} must contain label, domain, buffer, and seed"
        ));
    }
    let domain = match domain_name {
        "small" => SeedDomain::Small,
        "large" => SeedDomain::Large,
        other => {
            return Err(format!(
                "coverage seed line {line_number} has unknown domain {other:?}"
            ));
        }
    };
    let buffer_size = buffer_text
        .parse()
        .map_err(|error| format!("coverage seed line {line_number} has invalid buffer: {error}"))?;
    let seed = seed_text
        .parse()
        .map_err(|error| format!("coverage seed line {line_number} has invalid seed: {error}"))?;
    Ok(CoverageSeed {
        label: label.to_owned(),
        domain,
        buffer_size,
        seed,
    })
}
