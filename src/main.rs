use std::path::Path;
use std::sync::{Arc, Mutex};

use anyhow::Result;
use docker_update_monitor::{
    config::Config,
    health::HealthState,
    http::build_client,
    scanner::{run_check, BollardClient, DefaultScanOps},
    scheduler::run_scheduler,
    server::{build_router, AppState},
    state::{load_last_check, open_db},
};
use tokio::sync::mpsc;

#[tokio::main]
async fn main() -> Result<()> {
    if std::env::args().nth(1).as_deref() == Some("--health-check") {
        let port = std::env::var("WEB_PORT")
            .ok()
            .and_then(|v| v.parse::<u16>().ok())
            .unwrap_or(8080);
        let url = format!("http://127.0.0.1:{port}/health");
        let ok = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()?
            .get(&url)
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false);
        std::process::exit(if ok { 0 } else { 1 });
    }

    // 1. Init tracing (LOG_LEVEL env var, falls back to INFO)
    let log_level = std::env::var("LOG_LEVEL").unwrap_or_else(|_| "INFO".to_string());
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(&log_level)),
        )
        .init();

    // 2. Load config from environment
    let config = Config::from_env()?;

    // 3. Open SQLite + run migrations.
    // Two separate connections on the same WAL-mode file: one for the scanner
    // (owned by the main task, no mutex needed) and one for the Axum server
    // (shared across handler tasks via Arc<Mutex<>>).
    let db_path = Path::new(&config.state_db_path);
    let scan_conn = open_db(db_path)?;
    let server_conn = Arc::new(Mutex::new(open_db(db_path)?));

    // 4. Init health state; restore last_check from DB so the dashboard shows
    // the correct timestamp immediately on restart.
    let health = HealthState::new();
    if let Ok(Some(ts)) = load_last_check(&scan_conn) {
        health.update(Some(ts), None, None, None, None);
    }

    tracing::info!("Docker Update Monitor started");
    if config.dry_run {
        tracing::info!("DRY_RUN mode active — no HTTP POSTs will be made");
    }
    tracing::info!("Schedule: '{}'", config.cron_schedule);

    // 5. Channels: scan trigger (POST /api/scan → main loop) and shutdown
    let (scan_tx, scan_rx) = mpsc::channel::<()>(1);
    let (shutdown_tx, shutdown_rx) = mpsc::channel::<()>(1);

    // 6. Connect to Docker daemon
    let docker = BollardClient::connect()?;
    let http_client = build_client();
    let scan_ops = DefaultScanOps::new(http_client, config.clone());

    // 7. Spawn Axum server task
    let app_state = Arc::new(AppState {
        conn: server_conn,
        health: health.clone(),
        config: config.clone(),
    });
    let router = build_router(app_state, scan_tx);
    let addr = format!("0.0.0.0:{}", config.web_port);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!("Listening on {}", addr);
    tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });

    // Signal handlers: SIGINT (Ctrl-C) and SIGTERM both send on shutdown_tx
    let stx = shutdown_tx.clone();
    tokio::spawn(async move {
        tokio::signal::ctrl_c().await.ok();
        let _ = stx.send(()).await;
    });

    #[cfg(unix)]
    {
        let stx2 = shutdown_tx.clone();
        tokio::spawn(async move {
            use tokio::signal::unix::{signal, SignalKind};
            if let Ok(mut sig) = signal(SignalKind::terminate()) {
                sig.recv().await;
                let _ = stx2.send(()).await;
            }
        });
    }

    // 8. Main scheduling loop
    let cron_str = config.cron_schedule.clone();
    let health_sched = health.clone();
    run_scheduler(
        &cron_str,
        config.run_on_startup,
        scan_rx,
        shutdown_rx,
        move |next_dt| {
            health_sched.update(None, Some(next_dt), None, None, None);
            tracing::info!("Next check at: {}", next_dt.format("%Y-%m-%dT%H:%M:%S UTC"));
        },
        || async {
            if let Err(e) = run_check(&docker, &scan_ops, &scan_conn, &config, &health).await {
                tracing::error!("Check failed: {e}");
            }
        },
    )
    .await?;

    tracing::info!("Shutting down gracefully");
    Ok(())
}
