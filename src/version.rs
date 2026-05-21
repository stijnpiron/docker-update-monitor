use std::collections::HashMap;

use regex::Regex;

pub type Updates = HashMap<String, String>;

/// Matches `tag` against `pattern` (full-string match) and returns capture groups as u64.
/// Returns Err if the pattern has no capture groups (mirrors Python's ValueError).
/// Returns Ok(None) if the tag doesn't match or any group can't be parsed as u64.
pub fn parse_tag(tag: &str, pattern: &str) -> anyhow::Result<Option<Vec<u64>>> {
    let re = Regex::new(&format!("^(?:{pattern})$"))?;
    let Some(caps) = re.captures(tag) else {
        return Ok(None);
    };
    let group_count = caps.len() - 1;
    if group_count == 0 {
        anyhow::bail!(
            "Pattern '{pattern}' matched '{tag}' but has no capture groups — wrap each version number in ()"
        );
    }
    let parsed: Option<Vec<u64>> = (1..=group_count)
        .map(|i| caps.get(i).and_then(|m| m.as_str().parse::<u64>().ok()))
        .collect();
    Ok(parsed)
}

/// Compares a pre-parsed current version against a list of pre-parsed candidates.
/// Returns the best (highest) tag per update level: "patch", "minor", "major".
///
/// Adaptive comparison based on number of capture groups:
///   2 groups: minor (same major, higher minor), major (higher major)
///   3+ groups: patch (same maj+min, higher rest), minor (same maj), major
pub fn find_updates(current: &[u64], candidates: &[(&str, Vec<u64>)]) -> Updates {
    if current.len() < 2 {
        return Updates::new();
    }

    let num_groups = current.len();
    let mut best: HashMap<String, (Vec<u64>, String)> = HashMap::new();

    for (tag, v) in candidates {
        if v.len() < num_groups {
            continue;
        }

        let level = if num_groups == 2 {
            if v[0] == current[0] && v[1] > current[1] {
                "minor"
            } else if v[0] > current[0] {
                "major"
            } else {
                continue;
            }
        } else {
            if v[0] == current[0] && v[1] == current[1] && v[2..] > current[2..] {
                "patch"
            } else if v[0] == current[0] && v[1] > current[1] {
                "minor"
            } else if v[0] > current[0] {
                "major"
            } else {
                continue;
            }
        };

        let should_update = best.get(level).is_none_or(|(best_v, _)| v > best_v);
        if should_update {
            best.insert(level.to_string(), (v.clone(), (*tag).to_string()));
        }
    }

    best.into_iter()
        .map(|(level, (_, tag))| (level, tag))
        .collect()
}
