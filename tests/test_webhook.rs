use docker_update_monitor::config::Config;
use docker_update_monitor::models::{RegexMismatch, ScanWarning, UpdateInfo};
use docker_update_monitor::notifications::webhook::{build_payload, notify};
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn make_update(status: &str) -> UpdateInfo {
    UpdateInfo {
        container_name: "test-app".to_string(),
        service_name: "app".to_string(),
        stack: "mystack".to_string(),
        image: "nginx".to_string(),
        current_version: "1.0.0".to_string(),
        new_version: "1.1.0".to_string(),
        update_type: "minor".to_string(),
        status: status.to_string(),
        first_seen_at: None,
    }
}

fn make_mismatch() -> RegexMismatch {
    RegexMismatch {
        container_name: "app".to_string(),
        service_name: "app".to_string(),
        stack: "stack".to_string(),
        image: "nginx".to_string(),
        current_tag: "latest".to_string(),
        pattern: r"^\d+$".to_string(),
        reason: "did not match".to_string(),
    }
}

fn make_warning() -> ScanWarning {
    ScanWarning {
        container_name: "app".to_string(),
        image: "nginx".to_string(),
        level: "warning".to_string(),
        message: "fetch failed".to_string(),
    }
}

fn config_with_endpoint(endpoint: &str) -> Config {
    Config {
        notify_endpoint: Some(endpoint.to_string()),
        notify_auth_type: String::new(),
        notify_auth_token: String::new(),
        dry_run: false,
        notify_channels: "webhook".to_string(),
        cron_schedule: "0 * * * *".to_string(),
        label_prefix: "docker-update-monitor".to_string(),
        dockerhub_username: String::new(),
        dockerhub_password: String::new(),
        github_token: String::new(),
        run_on_startup: true,
        state_db_path: "/tmp/state.db".to_string(),
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
        update_cooldown: "0".to_string(),
    }
}

// --- build_payload tests ---

#[test]
fn test_build_payload_groups_by_status() {
    let updates = vec![
        make_update("new"),
        make_update("known"),
        make_update("resolved"),
    ];
    let payload = build_payload(&updates, &[], &[]);
    let obj = payload.as_object().unwrap();
    assert!(obj.contains_key("new"));
    assert!(obj.contains_key("known"));
    assert!(obj.contains_key("resolved"));
    assert_eq!(
        obj["new"].as_array().unwrap()[0]["container_name"],
        "test-app"
    );
}

#[test]
fn test_build_payload_omits_empty_groups() {
    let payload = build_payload(&[make_update("new")], &[], &[]);
    let obj = payload.as_object().unwrap();
    assert!(obj.contains_key("new"));
    assert!(!obj.contains_key("known"));
    assert!(!obj.contains_key("resolved"));
}

#[test]
fn test_build_payload_removes_status_field() {
    let payload = build_payload(&[make_update("new")], &[], &[]);
    let entries = payload["new"].as_array().unwrap();
    assert!(!entries[0].as_object().unwrap().contains_key("status"));
}

#[test]
fn test_build_payload_includes_mismatches() {
    let payload = build_payload(&[make_update("new")], &[make_mismatch()], &[]);
    let obj = payload.as_object().unwrap();
    assert!(obj.contains_key("regex_mismatches"));
    assert_eq!(
        obj["regex_mismatches"].as_array().unwrap()[0]["container_name"],
        "app"
    );
}

#[test]
fn test_build_payload_includes_warnings() {
    let payload = build_payload(&[make_update("new")], &[], &[make_warning()]);
    let obj = payload.as_object().unwrap();
    assert!(obj.contains_key("warnings"));
    assert_eq!(
        obj["warnings"].as_array().unwrap()[0]["message"],
        "fetch failed"
    );
}

// --- notify async tests ---

#[tokio::test]
async fn test_dry_run_does_not_post() {
    let server = MockServer::start().await;
    let mut config = config_with_endpoint(&server.uri());
    config.dry_run = true;

    let client = reqwest::Client::new();
    notify(&[make_update("new")], &[], &[], &client, &config)
        .await
        .unwrap();

    assert_eq!(server.received_requests().await.unwrap().len(), 0);
}

#[tokio::test]
async fn test_no_endpoint_does_not_post() {
    let server = MockServer::start().await;
    let mut config = config_with_endpoint(&server.uri());
    config.notify_endpoint = None;

    let client = reqwest::Client::new();
    notify(&[make_update("new")], &[], &[], &client, &config)
        .await
        .unwrap();

    assert_eq!(server.received_requests().await.unwrap().len(), 0);
}

#[tokio::test]
async fn test_successful_post_sends_json() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let mut config = config_with_endpoint(&server.uri());
    config.notify_endpoint = Some(format!("{}/", server.uri()));

    let client = reqwest::Client::new();
    notify(&[make_update("new")], &[], &[], &client, &config)
        .await
        .unwrap();

    let reqs = server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    let body: serde_json::Value = serde_json::from_slice(&reqs[0].body).unwrap();
    assert!(body.as_object().unwrap().contains_key("new"));
}

#[tokio::test]
async fn test_bearer_auth_header() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(header("authorization", "Bearer my-secret"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let mut config = config_with_endpoint(&server.uri());
    config.notify_endpoint = Some(format!("{}/", server.uri()));
    config.notify_auth_type = "bearer".to_string();
    config.notify_auth_token = "my-secret".to_string();

    let client = reqwest::Client::new();
    notify(&[make_update("new")], &[], &[], &client, &config)
        .await
        .unwrap();

    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn test_basic_auth_header() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(header("authorization", "Basic dXNlcjpwYXNz"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let mut config = config_with_endpoint(&server.uri());
    config.notify_endpoint = Some(format!("{}/", server.uri()));
    config.notify_auth_type = "basic".to_string();
    config.notify_auth_token = "dXNlcjpwYXNz".to_string();

    let client = reqwest::Client::new();
    notify(&[make_update("new")], &[], &[], &client, &config)
        .await
        .unwrap();

    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn test_no_auth_no_authorization_header() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let mut config = config_with_endpoint(&server.uri());
    config.notify_endpoint = Some(format!("{}/", server.uri()));

    let client = reqwest::Client::new();
    notify(&[make_update("new")], &[], &[], &client, &config)
        .await
        .unwrap();

    let reqs = server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    assert!(!reqs[0].headers.contains_key("authorization"));
}

#[tokio::test]
async fn test_unknown_auth_type_still_posts_without_auth_header() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&server)
        .await;

    let mut config = config_with_endpoint(&server.uri());
    config.notify_endpoint = Some(format!("{}/", server.uri()));
    config.notify_auth_type = "oauth2".to_string();
    config.notify_auth_token = "token".to_string();

    let client = reqwest::Client::new();
    notify(&[make_update("new")], &[], &[], &client, &config)
        .await
        .unwrap();

    let reqs = server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    assert!(!reqs[0].headers.contains_key("authorization"));
}

#[tokio::test]
async fn test_empty_updates_returns_early() {
    let server = MockServer::start().await;
    let config = config_with_endpoint(&server.uri());

    let client = reqwest::Client::new();
    notify(&[], &[], &[], &client, &config).await.unwrap();

    assert_eq!(server.received_requests().await.unwrap().len(), 0);
}

#[tokio::test]
async fn test_failed_post_does_not_propagate_error() {
    // No mock registered → server returns 404 by default; notify should not return Err
    let server = MockServer::start().await;
    let mut config = config_with_endpoint(&server.uri());
    config.notify_endpoint = Some(format!("{}/nonexistent", server.uri()));

    let client = reqwest::Client::new();
    let result = notify(&[make_update("new")], &[], &[], &client, &config).await;
    assert!(result.is_ok());
}
