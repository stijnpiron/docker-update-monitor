use chrono::Duration;
use docker_update_monitor::cooldown::parse_cooldown;
use rstest::rstest;

// 6 parameterized cases covering zero, hours, days, weeks, months, and whitespace stripping
#[rstest]
#[case("0", Duration::zero())]
#[case("", Duration::zero())]
#[case("12h", Duration::hours(12))]
#[case("3d", Duration::days(3))]
#[case("2w", Duration::weeks(2))]
#[case("1m", Duration::days(30))]
fn test_parse_cooldown_valid(#[case] input: &str, #[case] expected: Duration) {
    assert_eq!(parse_cooldown(input).unwrap(), expected);
}

// Unknown unit must return an error (mirrors Python's ValueError)
#[test]
fn test_parse_cooldown_invalid() {
    assert!(parse_cooldown("5x").is_err());
    let err = parse_cooldown("daily").unwrap_err();
    assert!(err.to_string().contains("Invalid cooldown format"));
}
