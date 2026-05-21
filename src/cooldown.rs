use chrono::Duration;
use regex::Regex;

/// Parses a cooldown string into a Duration.
///
/// Accepted formats: "0" or "" (no cooldown), "<n>h" (hours), "<n>d" (days),
/// "<n>w" (weeks), "<n>m" (months ≈ 30 days each).
pub fn parse_cooldown(s: &str) -> anyhow::Result<Duration> {
    let s = s.trim();
    if s.is_empty() || s == "0" {
        return Ok(Duration::zero());
    }

    let re = Regex::new(r"^(\d+)([hdwm])$").unwrap();
    let caps = re.captures(s).ok_or_else(|| {
        anyhow::anyhow!(
            "Invalid cooldown format '{}'. \
             Expected '0', or a positive integer followed by h/d/w/m (e.g. '12h', '3d', '2w', '1m').",
            s
        )
    })?;

    let amount: i64 = caps[1].parse()?;
    let duration = match &caps[2] {
        "h" => Duration::hours(amount),
        "d" => Duration::days(amount),
        "w" => Duration::weeks(amount),
        "m" => Duration::days(amount * 30),
        _ => unreachable!(),
    };
    Ok(duration)
}
