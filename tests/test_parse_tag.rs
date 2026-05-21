use docker_update_monitor::version::parse_tag;
use rstest::rstest;

// 5 parameterized cases covering: basic semver, v-prefix, two-group, no-match, non-integer groups
#[rstest]
#[case("1.2.3", r"^v?(\d+)\.(\d+)\.(\d+)$", Some(vec![1u64, 2, 3]))]
#[case("v1.2.3", r"^v?(\d+)\.(\d+)\.(\d+)$", Some(vec![1, 2, 3]))]
#[case("18.15", r"^(\d+)\.(\d+)$", Some(vec![18, 15]))]
#[case("latest", r"^(\d+)\.(\d+)\.(\d+)$", None)]
#[case("abc.def", r"^([a-z]+)\.([a-z]+)$", None)]
fn test_parse_tag(#[case] tag: &str, #[case] pattern: &str, #[case] expected: Option<Vec<u64>>) {
    assert_eq!(parse_tag(tag, pattern).unwrap(), expected);
}

// Pattern with no capture groups must return an error (mirrors Python's ValueError)
#[test]
fn test_parse_tag_no_groups_error() {
    let result = parse_tag("hello", r"^hello$");
    assert!(result.is_err());
    assert!(result
        .unwrap_err()
        .to_string()
        .contains("no capture groups"));
}
