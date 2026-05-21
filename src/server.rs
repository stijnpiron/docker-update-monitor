use std::sync::{Arc, Mutex};

use axum::{
    extract::State,
    http::{header, StatusCode},
    response::{Html, IntoResponse},
    routing::{get, post},
    Json, Router,
};
use chrono::NaiveDateTime;
use lazy_static::lazy_static;
use tera::Tera;
use tokio::sync::mpsc;
use tower_http::services::ServeDir;

use crate::{config::Config, health::HealthState, metrics::render_metrics, state::get_all_updates};

lazy_static! {
    static ref TERA: Tera = {
        let mut t = Tera::default();
        t.add_raw_template("index.html", include_str!("../templates/index.html"))
            .expect("Failed to parse index.html template");
        t
    };
}

pub struct AppState {
    pub conn: Arc<Mutex<rusqlite::Connection>>,
    pub health: HealthState,
    pub config: Config,
}

#[derive(Clone)]
struct RouterState {
    app: Arc<AppState>,
    scan_tx: mpsc::Sender<()>,
}

pub fn build_router(state: Arc<AppState>, scan_tx: mpsc::Sender<()>) -> Router {
    let rs = RouterState {
        app: state,
        scan_tx,
    };
    Router::new()
        .route("/", get(dashboard_handler))
        .route("/health", get(health_handler))
        .route("/metrics", get(metrics_handler))
        .route("/api/updates", get(updates_handler))
        .route("/api/last-scan", get(last_scan_handler))
        .route("/api/scan", post(scan_trigger_handler))
        .nest_service("/static", ServeDir::new("static"))
        .with_state(rs)
}

fn format_datetime(s: &str, fmt: &str, tz: &str) -> String {
    if s.is_empty() {
        return "\u{2014}".to_string();
    }
    let dt_utc = if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(s) {
        Some(dt.with_timezone(&chrono::Utc))
    } else if let Ok(ndt) = NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S") {
        Some(ndt.and_utc())
    } else {
        None
    };

    if let Some(dt) = dt_utc {
        if !tz.is_empty() {
            if let Ok(parsed_tz) = tz.parse::<chrono_tz::Tz>() {
                return dt.with_timezone(&parsed_tz).format(fmt).to_string();
            }
            tracing::warn!(
                "Unknown timezone {:?} in TZ env var, falling back to UTC",
                tz
            );
        }
        return dt.format(fmt).to_string();
    }
    s.to_string()
}

async fn dashboard_handler(State(s): State<RouterState>) -> impl IntoResponse {
    let app = &s.app;
    let fmt = &app.config.dashboard_datetime_format;
    let tz = &app.config.tz;

    let updates = {
        let conn = app.conn.lock().unwrap();
        get_all_updates(&conn).unwrap_or_default()
    };

    let mut updates_json: Vec<serde_json::Value> = updates
        .iter()
        .map(|u| {
            let mut v = serde_json::to_value(u).unwrap_or_default();
            let display = format_datetime(&u.first_seen_at, fmt, tz);
            if let Some(obj) = v.as_object_mut() {
                obj.insert(
                    "first_seen_at_display".to_string(),
                    serde_json::Value::String(display),
                );
            }
            v
        })
        .collect();

    updates_json.sort_by(|a, b| {
        let sa = a["stack"].as_str().unwrap_or("");
        let sb = b["stack"].as_str().unwrap_or("");
        let ca = a["container_name"].as_str().unwrap_or("");
        let cb = b["container_name"].as_str().unwrap_or("");
        sa.cmp(sb).then(ca.cmp(cb))
    });

    let pending_updates: Vec<&serde_json::Value> = updates_json
        .iter()
        .filter(|u| u["status"].as_str() != Some("resolved"))
        .collect();
    let resolved_updates: Vec<&serde_json::Value> = updates_json
        .iter()
        .filter(|u| u["status"].as_str() == Some("resolved"))
        .collect();

    let new_count = updates_json
        .iter()
        .filter(|u| u["status"].as_str() == Some("new"))
        .count();
    let known_count = updates_json
        .iter()
        .filter(|u| u["status"].as_str() == Some("known"))
        .count();
    let resolved_count = resolved_updates.len();

    let health_json = app.health.to_json();
    let last_check_raw = health_json["last_check"].as_str().unwrap_or("").to_string();
    let next_check_raw = health_json["next_check"].as_str().unwrap_or("").to_string();
    let containers_monitored = health_json["containers_monitored"].as_u64().unwrap_or(0);

    let last_check_display = if last_check_raw.is_empty() {
        "never".to_string()
    } else {
        format_datetime(&last_check_raw, fmt, tz)
    };
    let next_check_display = if next_check_raw.is_empty() {
        "\u{2014}".to_string()
    } else {
        format_datetime(&next_check_raw, fmt, tz)
    };

    let warnings = app.health.warnings();
    let skipped_containers = app.health.skipped_containers();

    let mut ctx = tera::Context::new();
    ctx.insert("updates", &updates_json);
    ctx.insert("pending_updates", &pending_updates);
    ctx.insert("resolved_updates", &resolved_updates);
    ctx.insert("last_check", &last_check_display);
    ctx.insert("last_check_raw", &last_check_raw);
    ctx.insert("next_check", &next_check_display);
    ctx.insert("containers_monitored", &containers_monitored);
    ctx.insert("new_count", &new_count);
    ctx.insert("known_count", &known_count);
    ctx.insert("resolved_count", &resolved_count);
    ctx.insert("warnings", &warnings);
    ctx.insert("skipped_containers", &skipped_containers);

    match TERA.render("index.html", &ctx) {
        Ok(html) => Html(html).into_response(),
        Err(e) => {
            tracing::error!("Template render error: {e}");
            (StatusCode::INTERNAL_SERVER_ERROR, "Template error").into_response()
        }
    }
}

async fn health_handler(State(s): State<RouterState>) -> impl IntoResponse {
    let health = &s.app.health;
    let status = if health.is_docker_healthy() {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (status, Json(health.to_json()))
}

async fn metrics_handler() -> impl IntoResponse {
    let body = render_metrics();
    (
        [(
            header::CONTENT_TYPE,
            "text/plain; version=0.0.4; charset=utf-8",
        )],
        body,
    )
}

async fn updates_handler(State(s): State<RouterState>) -> impl IntoResponse {
    let updates = {
        let conn = s.app.conn.lock().unwrap();
        get_all_updates(&conn).unwrap_or_default()
    };
    Json(updates)
}

async fn last_scan_handler(State(s): State<RouterState>) -> impl IntoResponse {
    let health_json = s.app.health.to_json();
    let last_check = health_json["last_check"].clone();
    Json(serde_json::json!({ "last_check": last_check }))
}

async fn scan_trigger_handler(State(s): State<RouterState>) -> impl IntoResponse {
    let _ = s.scan_tx.try_send(());
    (
        StatusCode::ACCEPTED,
        Json(serde_json::json!({ "message": "Scan triggered" })),
    )
}
