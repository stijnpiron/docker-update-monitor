use anyhow::Result;
use chrono::{DateTime, Utc};
use cron::Schedule;
use std::str::FromStr;
use tokio::sync::mpsc;

/// Convert a standard 5-field Unix cron expression to the 7-field format
/// required by the `cron` crate (sec min hour day month weekday year).
pub fn normalize_cron(expr: &str) -> String {
    let fields: Vec<&str> = expr.split_whitespace().collect();
    match fields.len() {
        5 => format!("0 {} *", expr),
        6 => format!("{} *", expr),
        _ => expr.to_string(),
    }
}

pub fn parse_schedule(cron_str: &str) -> Result<Schedule> {
    let normalized = normalize_cron(cron_str);
    Schedule::from_str(&normalized)
        .map_err(|e| anyhow::anyhow!("Invalid cron expression '{}': {}", cron_str, e))
}

/// Returns the next scheduled `DateTime<Utc>` for the given `Schedule`.
pub fn next_run(schedule: &Schedule) -> Option<DateTime<Utc>> {
    schedule.upcoming(Utc).next()
}

/// Drives the cron scheduling loop.
///
/// Calls `run_check` on startup (if `run_on_startup`) then loops, firing on
/// the cron schedule or when `scan_rx` receives a manual trigger.  Exits
/// cleanly when `shutdown_rx` receives a message.
///
/// `on_next` is called with the next scheduled `DateTime<Utc>` at the start
/// of each loop iteration, giving the caller a chance to update health state
/// and emit a log line.
pub async fn run_scheduler<F, Fut>(
    cron_str: &str,
    run_on_startup: bool,
    mut scan_rx: mpsc::Receiver<()>,
    mut shutdown_rx: mpsc::Receiver<()>,
    mut on_next: impl FnMut(DateTime<Utc>),
    run_check: F,
) -> Result<()>
where
    F: Fn() -> Fut,
    Fut: std::future::Future<Output = ()>,
{
    let schedule = parse_schedule(cron_str)?;

    if run_on_startup {
        run_check().await;
    }

    while let Some(next_dt) = next_run(&schedule) {
        on_next(next_dt);

        let now = Utc::now();
        let delay = (next_dt - now).to_std().unwrap_or_default();

        enum Event {
            Scheduled,
            Manual,
            Shutdown,
        }

        // `Some(())` pattern: if the scan channel is closed (sender dropped) the
        // pattern won't match and that branch is disabled — the select continues
        // waiting on the other two branches instead of looping on a closed channel.
        let event = tokio::select! {
            _ = tokio::time::sleep(delay) => Event::Scheduled,
            Some(()) = scan_rx.recv() => Event::Manual,
            _ = shutdown_rx.recv() => Event::Shutdown,
        };

        match event {
            Event::Scheduled => run_check().await,
            Event::Manual => {
                tracing::info!("Manual scan triggered via dashboard");
                run_check().await;
            }
            Event::Shutdown => break,
        }
    }

    Ok(())
}
