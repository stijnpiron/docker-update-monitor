use std::collections::{HashMap, HashSet};
use std::sync::Mutex;

use lazy_static::lazy_static;
use prometheus::{Counter, CounterVec, Encoder, Gauge, GaugeVec, TextEncoder};

use crate::models::UpdateInfo;

lazy_static! {
    pub static ref CONTAINERS_MONITORED: Gauge = prometheus::register_gauge!(
        "dum_containers_monitored",
        "Number of containers with update-monitor labels"
    )
    .unwrap();
    pub static ref UPDATES_AVAILABLE: GaugeVec = prometheus::register_gauge_vec!(
        "dum_updates_available",
        "Number of available updates by type",
        &["type"]
    )
    .unwrap();
    pub static ref CHECK_DURATION: Gauge = prometheus::register_gauge!(
        "dum_check_duration_seconds",
        "Duration of the last update check in seconds"
    )
    .unwrap();
    pub static ref LAST_CHECK_TIMESTAMP: Gauge = prometheus::register_gauge!(
        "dum_last_check_timestamp_seconds",
        "Unix timestamp of last completed check"
    )
    .unwrap();
    pub static ref CHECK_ERRORS: Counter = prometheus::register_counter!(
        "dum_check_errors_total",
        "Total number of errors during checks"
    )
    .unwrap();
    pub static ref NOTIFICATIONS_SENT: CounterVec = prometheus::register_counter_vec!(
        "dum_notifications_sent_total",
        "Total notifications sent by channel",
        &["channel"]
    )
    .unwrap();
    static ref SEEN_UPDATE_TYPES: Mutex<HashSet<String>> = Mutex::new(HashSet::new());
}

pub fn init() {
    lazy_static::initialize(&CONTAINERS_MONITORED);
    lazy_static::initialize(&UPDATES_AVAILABLE);
    lazy_static::initialize(&CHECK_DURATION);
    lazy_static::initialize(&LAST_CHECK_TIMESTAMP);
    lazy_static::initialize(&CHECK_ERRORS);
    lazy_static::initialize(&NOTIFICATIONS_SENT);
    lazy_static::initialize(&SEEN_UPDATE_TYPES);
}

pub fn update_after_scan(
    monitored: u64,
    updates: &[UpdateInfo],
    duration_seconds: f64,
    last_check_ts: f64,
) {
    CONTAINERS_MONITORED.set(monitored as f64);
    CHECK_DURATION.set(duration_seconds);
    LAST_CHECK_TIMESTAMP.set(last_check_ts);

    let mut by_type: HashMap<String, f64> = HashMap::new();
    for u in updates {
        if u.status != "resolved" {
            *by_type.entry(u.update_type.clone()).or_insert(0.0) += 1.0;
        }
    }

    let mut seen = SEEN_UPDATE_TYPES.lock().unwrap_or_else(|p| p.into_inner());
    for stale_type in seen.iter() {
        if !by_type.contains_key(stale_type) {
            UPDATES_AVAILABLE.with_label_values(&[stale_type]).set(0.0);
        }
    }
    for (t, count) in &by_type {
        UPDATES_AVAILABLE.with_label_values(&[t]).set(*count);
    }
    *seen = by_type.into_keys().collect();
}

pub fn render_metrics() -> String {
    let encoder = TextEncoder::new();
    let metric_families = prometheus::gather();
    let mut buffer = Vec::new();
    if encoder.encode(&metric_families, &mut buffer).is_err() {
        return String::new();
    }
    String::from_utf8(buffer).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    static TEST_LOCK: Mutex<()> = Mutex::new(());

    fn make_update(update_type: &str, status: &str) -> UpdateInfo {
        UpdateInfo {
            container_name: "c".to_string(),
            service_name: "s".to_string(),
            stack: "st".to_string(),
            image: "img".to_string(),
            current_version: "1.0".to_string(),
            new_version: "2.0".to_string(),
            update_type: update_type.to_string(),
            status: status.to_string(),
            first_seen_at: None,
        }
    }

    #[test]
    fn test_render_metrics_contains_expected_names() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        init();
        // Populate the vec metrics so they appear in output (Rust prometheus crate
        // only emits GaugeVec/CounterVec entries once a label combination is accessed)
        UPDATES_AVAILABLE
            .with_label_values(&["render_test"])
            .set(0.0);
        NOTIFICATIONS_SENT
            .with_label_values(&["render_test"])
            .inc_by(0.0);
        let output = render_metrics();
        assert!(output.contains("dum_containers_monitored"));
        assert!(output.contains("dum_updates_available"));
        assert!(output.contains("dum_check_duration_seconds"));
        assert!(output.contains("dum_check_errors_total"));
        assert!(output.contains("dum_last_check_timestamp_seconds"));
        assert!(output.contains("dum_notifications_sent_total"));
    }

    #[test]
    fn test_update_after_scan_sets_gauges() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        update_after_scan(7, &[], 14.2, 1_714_300_800.0);
        assert_eq!(CONTAINERS_MONITORED.get(), 7.0);
        assert!((CHECK_DURATION.get() - 14.2).abs() < 1e-6);
        assert_eq!(LAST_CHECK_TIMESTAMP.get(), 1_714_300_800.0);
    }

    #[test]
    fn test_update_after_scan_counts_by_type() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let updates = vec![
            make_update("minor", "new"),
            make_update("minor", "known"),
            make_update("major", "new"),
            make_update("major", "resolved"),
        ];
        update_after_scan(4, &updates, 0.0, 0.0);
        assert_eq!(UPDATES_AVAILABLE.with_label_values(&["minor"]).get(), 2.0);
        assert_eq!(UPDATES_AVAILABLE.with_label_values(&["major"]).get(), 1.0);
    }

    #[test]
    fn test_update_after_scan_zeroes_stale_types() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        update_after_scan(1, &[make_update("patch", "new")], 0.0, 0.0);
        assert_eq!(UPDATES_AVAILABLE.with_label_values(&["patch"]).get(), 1.0);
        update_after_scan(0, &[], 0.0, 0.0);
        assert_eq!(UPDATES_AVAILABLE.with_label_values(&["patch"]).get(), 0.0);
    }
}
