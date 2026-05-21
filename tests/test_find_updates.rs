use docker_update_monitor::version::{find_updates, parse_tag};

fn parse_candidates<'a>(tags: &'a [&'a str], pattern: &str) -> Vec<(&'a str, Vec<u64>)> {
    tags.iter()
        .filter_map(|tag| parse_tag(tag, pattern).ok().flatten().map(|v| (*tag, v)))
        .collect()
}

fn parse_current(tag: &str, pattern: &str) -> Vec<u64> {
    parse_tag(tag, pattern).unwrap().unwrap()
}

#[test]
fn test_two_group_minor_update() {
    let pattern = r"^(\d+)\.(\d+)$";
    let current = parse_current("18.15", pattern);
    let tags = ["18.20", "18.10", "17.0"];
    let candidates = parse_candidates(&tags, pattern);
    let result = find_updates(&current, &candidates);
    assert_eq!(result.get("minor").map(String::as_str), Some("18.20"));
    assert!(!result.contains_key("major"));
    assert!(!result.contains_key("patch"));
}

#[test]
fn test_three_group_patch_update() {
    let pattern = r"^(\d+)\.(\d+)\.(\d+)$";
    let current = parse_current("1.2.3", pattern);
    let tags = ["1.2.4", "1.2.5", "1.2.2"];
    let candidates = parse_candidates(&tags, pattern);
    let result = find_updates(&current, &candidates);
    assert_eq!(result.get("patch").map(String::as_str), Some("1.2.5"));
    assert!(!result.contains_key("minor"));
    assert!(!result.contains_key("major"));
}

#[test]
fn test_three_group_all_levels() {
    let pattern = r"^(\d+)\.(\d+)\.(\d+)$";
    let current = parse_current("1.2.3", pattern);
    let tags = ["1.2.5", "1.3.0", "2.0.0", "latest", "alpine"];
    let candidates = parse_candidates(&tags, pattern);
    let result = find_updates(&current, &candidates);
    assert_eq!(result.get("patch").map(String::as_str), Some("1.2.5"));
    assert_eq!(result.get("minor").map(String::as_str), Some("1.3.0"));
    assert_eq!(result.get("major").map(String::as_str), Some("2.0.0"));
}

#[test]
fn test_no_updates() {
    let pattern = r"^(\d+)\.(\d+)\.(\d+)$";
    let current = parse_current("1.2.3", pattern);
    let tags = ["1.2.3", "1.2.2", "0.9.9"];
    let candidates = parse_candidates(&tags, pattern);
    let result = find_updates(&current, &candidates);
    assert!(result.is_empty());
}
