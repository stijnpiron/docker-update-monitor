use std::sync::{Arc, RwLock};

use chrono::{DateTime, Utc};

use crate::models::{ScanWarning, SkippedContainer};

#[derive(Clone)]
pub struct HealthState {
    inner: Arc<RwLock<HealthStateInner>>,
}

struct HealthStateInner {
    last_check: Option<DateTime<Utc>>,
    next_check: Option<DateTime<Utc>>,
    containers_monitored: usize,
    warnings: Vec<ScanWarning>,
    skipped_containers: Vec<SkippedContainer>,
    started_at: DateTime<Utc>,
}

impl HealthState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(RwLock::new(HealthStateInner {
                last_check: None,
                next_check: None,
                containers_monitored: 0,
                warnings: Vec::new(),
                skipped_containers: Vec::new(),
                started_at: Utc::now(),
            })),
        }
    }

    pub fn update(
        &self,
        last_check: Option<DateTime<Utc>>,
        next_check: Option<DateTime<Utc>>,
        containers_monitored: Option<usize>,
        warnings: Option<Vec<ScanWarning>>,
        skipped_containers: Option<Vec<SkippedContainer>>,
    ) {
        let mut state = self.inner.write().unwrap();
        if let Some(lc) = last_check {
            state.last_check = Some(lc);
        }
        if let Some(nc) = next_check {
            state.next_check = Some(nc);
        }
        if let Some(cm) = containers_monitored {
            state.containers_monitored = cm;
        }
        if let Some(w) = warnings {
            state.warnings = w;
        }
        if let Some(sc) = skipped_containers {
            state.skipped_containers = sc;
        }
    }

    pub fn to_json(&self) -> serde_json::Value {
        let state = self.inner.read().unwrap();
        let uptime = (Utc::now() - state.started_at).num_seconds().max(0) as u64;

        let last_check_str = state
            .last_check
            .map(|dt| dt.format("%Y-%m-%dT%H:%M:%SZ").to_string());
        let next_check_str = state
            .next_check
            .map(|dt| dt.format("%Y-%m-%dT%H:%M:%SZ").to_string());

        if state.last_check.is_none() {
            serde_json::json!({
                "status": "starting",
                "last_check": serde_json::Value::Null,
                "next_check": next_check_str,
                "containers_monitored": state.containers_monitored,
                "uptime_seconds": uptime,
                "note": "waiting for first scan to complete"
            })
        } else {
            serde_json::json!({
                "status": "ok",
                "last_check": last_check_str,
                "next_check": next_check_str,
                "containers_monitored": state.containers_monitored,
                "uptime_seconds": uptime
            })
        }
    }

    pub fn warnings(&self) -> Vec<ScanWarning> {
        self.inner.read().unwrap().warnings.clone()
    }

    pub fn skipped_containers(&self) -> Vec<SkippedContainer> {
        self.inner.read().unwrap().skipped_containers.clone()
    }
}

impl Default for HealthState {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn test_initial_state_is_starting() {
        let hs = HealthState::new();
        let j = hs.to_json();
        assert_eq!(j["status"], "starting");
        assert!(j["note"]
            .as_str()
            .unwrap()
            .contains("waiting for first scan"));
        assert_eq!(j["last_check"], serde_json::Value::Null);
        assert!(j["uptime_seconds"].as_u64().is_some());
    }

    #[test]
    fn test_update_sets_fields_and_to_json() {
        let hs = HealthState::new();
        let last_check = Utc.with_ymd_and_hms(2026, 4, 28, 3, 0, 0).unwrap();
        let next_check = Utc.with_ymd_and_hms(2026, 5, 5, 3, 0, 0).unwrap();
        hs.update(Some(last_check), Some(next_check), Some(12), None, None);
        let j = hs.to_json();
        assert_eq!(j["status"], "ok");
        assert_eq!(j["last_check"], "2026-04-28T03:00:00Z");
        assert_eq!(j["next_check"], "2026-05-05T03:00:00Z");
        assert_eq!(j["containers_monitored"], 12);
        assert!(j["uptime_seconds"].as_u64().is_some());
    }
}
