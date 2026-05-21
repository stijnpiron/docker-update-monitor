use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use docker_update_monitor::{
    config::Config,
    health::HealthState,
    models::UpdateInfo,
    server::{build_router, AppState},
    state::{open_db, process_scan},
};
use http_body_util::BodyExt;
use tempfile::NamedTempFile;
use tower::util::ServiceExt;

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

fn test_config() -> Config {
    Config {
        label_prefix: "docker-update-monitor".to_string(),
        update_cooldown: "0".to_string(),
        github_token: String::new(),
        dockerhub_username: String::new(),
        dockerhub_password: String::new(),
        notify_channels: "webhook".to_string(),
        notify_endpoint: None,
        notify_auth_type: String::new(),
        notify_auth_token: String::new(),
        dry_run: false,
        state_db_path: String::new(),
        cron_schedule: "0 * * * *".to_string(),
        run_on_startup: false,
        log_level: "INFO".to_string(),
        smtp_host: String::new(),
        smtp_port: 587,
        smtp_username: String::new(),
        smtp_password: String::new(),
        smtp_from: String::new(),
        smtp_to: String::new(),
        smtp_tls: true,
        web_port: 8080,
        dashboard_datetime_format: "%d/%m/%Y %H:%M".to_string(),
        tz: String::new(),
    }
}

fn make_app(
    conn: rusqlite::Connection,
    health: HealthState,
) -> (axum::Router, tokio::sync::mpsc::Receiver<()>) {
    let (scan_tx, scan_rx) = tokio::sync::mpsc::channel(4);
    let state = Arc::new(AppState {
        conn: Arc::new(Mutex::new(conn)),
        health,
        config: test_config(),
    });
    (build_router(state, scan_tx), scan_rx)
}

async fn body_bytes(resp: axum::response::Response) -> Vec<u8> {
    resp.into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes()
        .to_vec()
}

async fn body_string(resp: axum::response::Response) -> String {
    String::from_utf8(body_bytes(resp).await).unwrap()
}

fn insert_update(conn: &rusqlite::Connection, container_name: &str, status: &str) {
    let update = UpdateInfo {
        container_name: container_name.to_string(),
        service_name: String::new(),
        stack: "mystack".to_string(),
        image: "nginx".to_string(),
        current_version: "1.0.0".to_string(),
        new_version: "2.0.0".to_string(),
        update_type: "major".to_string(),
        status: status.to_string(),
        first_seen_at: None,
    };
    process_scan(conn, &[update], None, None, None).unwrap();
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_health_starting() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&body_bytes(resp).await[..]).unwrap();
    assert_eq!(body["status"], "starting");
}

#[tokio::test]
async fn test_health_ok_after_scan() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let health = HealthState::new();
    health.update(Some(chrono::Utc::now()), None, Some(3), None, None);
    let (app, _rx) = make_app(conn, health);

    let resp = app
        .oneshot(Request::get("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&body_bytes(resp).await[..]).unwrap();
    assert_eq!(body["status"], "ok");
    assert_eq!(body["containers_monitored"], 3);
}

#[tokio::test]
async fn test_api_updates_empty() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/api/updates").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&body_bytes(resp).await[..]).unwrap();
    assert!(body.as_array().unwrap().is_empty());
}

#[tokio::test]
async fn test_api_updates_returns_records() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    insert_update(&conn, "my-app", "new");
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/api/updates").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&body_bytes(resp).await[..]).unwrap();
    let arr = body.as_array().unwrap();
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["container_name"], "my-app");
    assert_eq!(arr[0]["status"], "new");
}

#[tokio::test]
async fn test_api_scan_returns_202() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::post("/api/scan").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::ACCEPTED);
    let body: serde_json::Value = serde_json::from_slice(&body_bytes(resp).await[..]).unwrap();
    let msg = body["message"].as_str().unwrap().to_lowercase();
    assert!(msg.contains("scan") || msg.contains("trigger"));
}

#[tokio::test]
async fn test_api_scan_sends_on_channel() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let (app, mut scan_rx) = make_app(conn, HealthState::new());

    app.oneshot(Request::post("/api/scan").body(Body::empty()).unwrap())
        .await
        .unwrap();

    assert!(
        scan_rx.try_recv().is_ok(),
        "scan channel should have received a message"
    );
}

#[tokio::test]
async fn test_api_scan_get_not_allowed() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/api/scan").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn test_api_last_scan_null() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/api/last-scan").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&body_bytes(resp).await[..]).unwrap();
    assert_eq!(body["last_check"], serde_json::Value::Null);
}

#[tokio::test]
async fn test_api_last_scan_timestamp() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let health = HealthState::new();
    let ts = chrono::DateTime::parse_from_rfc3339("2026-04-30T12:00:00Z")
        .unwrap()
        .with_timezone(&chrono::Utc);
    health.update(Some(ts), None, None, None, None);
    let (app, _rx) = make_app(conn, health);

    let resp = app
        .oneshot(Request::get("/api/last-scan").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&body_bytes(resp).await[..]).unwrap();
    let last_check = body["last_check"].as_str().unwrap();
    assert!(last_check.contains("2026-04-30"));
}

#[tokio::test]
async fn test_dashboard_renders() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    insert_update(&conn, "nginx-web", "new");
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let html = body_string(resp).await;
    assert!(html.contains("Docker Update Monitor"), "title missing");
    assert!(html.contains("nginx-web"), "container name missing");
    assert!(html.contains("mystack"), "stack missing");
    assert!(html.contains("id=\"update-banner\""), "banner missing");
    assert!(html.contains("data-last-check"), "data attribute missing");
}

#[tokio::test]
async fn test_dashboard_empty_state() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let html = body_string(resp).await;
    assert!(html.contains("No updates found"));
}

#[tokio::test]
async fn test_dashboard_xss_prevention() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    let update = UpdateInfo {
        container_name: "<script>alert(1)</script>".to_string(),
        service_name: String::new(),
        stack: "s".to_string(),
        image: "img".to_string(),
        current_version: "1.0".to_string(),
        new_version: "2.0".to_string(),
        update_type: "major".to_string(),
        status: "new".to_string(),
        first_seen_at: None,
    };
    process_scan(&conn, &[update], None, None, None).unwrap();
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let html = body_string(resp).await;
    assert!(
        !html.contains("<script>alert(1)</script>"),
        "raw script tag must not appear in HTML"
    );
    assert!(
        html.contains("&lt;script&gt;"),
        "HTML-escaped script tag expected"
    );
}

#[tokio::test]
async fn test_dashboard_tz_conversion() {
    use std::sync::Arc;
    use std::sync::Mutex;

    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    insert_update(&conn, "tz-app", "new");

    let (scan_tx, _rx) = tokio::sync::mpsc::channel(4);
    let mut cfg = test_config();
    cfg.tz = "Europe/Brussels".to_string();
    cfg.dashboard_datetime_format = "%H:%M".to_string();
    let state = Arc::new(docker_update_monitor::server::AppState {
        conn: Arc::new(Mutex::new(conn)),
        health: HealthState::new(),
        config: cfg,
    });
    let app = docker_update_monitor::server::build_router(state, scan_tx);

    let resp = app
        .oneshot(Request::get("/").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let html = body_string(resp).await;
    assert!(
        html.contains("tz-app"),
        "container name missing from tz test"
    );
}

#[tokio::test]
async fn test_metrics_prometheus_format() {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    docker_update_monitor::metrics::init();
    let (app, _rx) = make_app(conn, HealthState::new());

    let resp = app
        .oneshot(Request::get("/metrics").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = body_string(resp).await;
    assert!(body.contains("dum_containers_monitored"), "metric missing");
    assert!(body.contains("dum_check_errors_total"), "metric missing");
    // Validate basic Prometheus text format: lines of "metric_name ..."
    let has_metric_lines = body.lines().any(|l| !l.starts_with('#') && l.contains(' '));
    assert!(has_metric_lines, "no metric value lines found");
}
