use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use std::time::Duration;

use docker_update_monitor::scheduler::{normalize_cron, parse_schedule, run_scheduler};
use tokio::sync::mpsc;

// ── helper ────────────────────────────────────────────────────────────────────

/// Counter closure factory: returns a closure that increments an `Arc<AtomicUsize>`
/// and returns a ready future, suitable for use as the `run_check` argument.
macro_rules! counter_check {
    ($arc:expr) => {{
        let cc = $arc.clone();
        move || {
            let cc = cc.clone();
            async move {
                cc.fetch_add(1, Ordering::SeqCst);
            }
        }
    }};
}

// ── 1. normalize_cron ────────────────────────────────────────────────────────

#[test]
fn test_normalize_cron_5_field() {
    // Standard Unix "0 * * * *" becomes 7-field for the cron crate
    assert_eq!(normalize_cron("0 * * * *"), "0 0 * * * * *");
}

#[test]
fn test_normalize_cron_7_field_unchanged() {
    assert_eq!(normalize_cron("* * * * * * *"), "* * * * * * *");
}

// ── 2. parse_schedule ────────────────────────────────────────────────────────

#[test]
fn test_parse_schedule_valid() {
    // 5-field standard cron
    assert!(parse_schedule("0 * * * *").is_ok());
}

#[test]
fn test_parse_schedule_invalid_returns_error() {
    assert!(parse_schedule("not a cron expression").is_err());
}

// ── 3. run_on_startup ────────────────────────────────────────────────────────

#[tokio::test]
async fn test_run_on_startup_calls_check_once() {
    let count = Arc::new(AtomicUsize::new(0));

    let (_scan_tx, scan_rx) = mpsc::channel(1);
    let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>(1);

    // Pre-send shutdown so the loop exits immediately after the startup check
    let _ = shutdown_tx.try_send(());

    run_scheduler(
        "* * * * * * *",
        true,
        scan_rx,
        shutdown_rx,
        |_| {},
        counter_check!(count),
    )
    .await
    .unwrap();

    assert_eq!(
        count.load(Ordering::SeqCst),
        1,
        "startup check must run exactly once"
    );
}

#[tokio::test]
async fn test_no_run_on_startup_skips_initial_check() {
    let count = Arc::new(AtomicUsize::new(0));

    let (_scan_tx, scan_rx) = mpsc::channel(1);
    let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>(1);

    // Shut down before any cron tick fires
    let _ = shutdown_tx.try_send(());

    run_scheduler(
        "* * * * * * *",
        false,
        scan_rx,
        shutdown_rx,
        |_| {},
        counter_check!(count),
    )
    .await
    .unwrap();

    assert_eq!(
        count.load(Ordering::SeqCst),
        0,
        "no check should run when run_on_startup=false and shutdown is immediate"
    );
}

// ── 4. manual trigger ────────────────────────────────────────────────────────

#[tokio::test]
async fn test_manual_trigger_fires_check() {
    let count = Arc::new(AtomicUsize::new(0));

    let (scan_tx, scan_rx) = mpsc::channel(1);
    let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>(1);

    // Send manual trigger before the scheduler starts its first sleep
    let _ = scan_tx.try_send(());

    // The check closure also sends shutdown so we exit after exactly one scan
    let count_c = count.clone();
    let stx = shutdown_tx.clone();
    run_scheduler(
        "0 * * * *", // hourly — won't fire naturally in this test
        false,
        scan_rx,
        shutdown_rx,
        |_| {},
        move || {
            let count_c = count_c.clone();
            let stx = stx.clone();
            async move {
                count_c.fetch_add(1, Ordering::SeqCst);
                let _ = stx.try_send(());
            }
        },
    )
    .await
    .unwrap();

    assert_eq!(
        count.load(Ordering::SeqCst),
        1,
        "manual trigger must fire exactly one check"
    );
}

// ── 5. shutdown exits the loop cleanly ───────────────────────────────────────

#[tokio::test]
async fn test_shutdown_exits_loop() {
    let count = Arc::new(AtomicUsize::new(0));

    let (_scan_tx, scan_rx) = mpsc::channel(1);
    let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>(1);

    // Shutdown while the scheduler is sleeping for the hourly cron
    let stx = shutdown_tx.clone();
    tokio::spawn(async move {
        // Yield so the scheduler enters its select before we cancel it
        tokio::task::yield_now().await;
        let _ = stx.send(()).await;
    });

    run_scheduler(
        "0 * * * *", // ~1 hour until next tick — shutdown comes first
        false,
        scan_rx,
        shutdown_rx,
        |_| {},
        counter_check!(count),
    )
    .await
    .unwrap();

    assert_eq!(
        count.load(Ordering::SeqCst),
        0,
        "shutdown must prevent any check from running"
    );
}

// ── 6. scheduled check fires after cron period ───────────────────────────────

#[tokio::test]
async fn test_scheduled_check_fires_after_cron_period() {
    let count = Arc::new(AtomicUsize::new(0));

    let (_, scan_rx) = mpsc::channel(1);
    let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>(1);

    // Shut down after 1.5 s — enough time for at least one "every second" tick
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(1500)).await;
        let _ = shutdown_tx.send(()).await;
    });

    run_scheduler(
        "* * * * * * *", // every second
        false,
        scan_rx,
        shutdown_rx,
        |_| {},
        counter_check!(count),
    )
    .await
    .unwrap();

    assert!(
        count.load(Ordering::SeqCst) >= 1,
        "at least one scheduled check must fire within 1.5 s with a per-second cron"
    );
}

// ── 7. on_next is called with a future timestamp ──────────────────────────────

#[tokio::test]
async fn test_on_next_receives_future_timestamp() {
    use chrono::Utc;

    let before = Utc::now();
    let received: Arc<std::sync::Mutex<Option<chrono::DateTime<Utc>>>> =
        Arc::new(std::sync::Mutex::new(None));

    let (_scan_tx, scan_rx) = mpsc::channel(1);
    let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>(1);
    let _ = shutdown_tx.try_send(());

    let rec = received.clone();
    run_scheduler(
        "* * * * * * *",
        false,
        scan_rx,
        shutdown_rx,
        move |dt| {
            *rec.lock().unwrap() = Some(dt);
        },
        || async {},
    )
    .await
    .unwrap();

    let ts = received
        .lock()
        .unwrap()
        .expect("on_next must have been called");
    assert!(
        ts > before,
        "on_next must receive a timestamp in the future"
    );
}
