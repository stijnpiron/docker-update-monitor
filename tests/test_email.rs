use docker_update_monitor::config::Config;
use docker_update_monitor::models::{RegexMismatch, ScanWarning, UpdateInfo};
use docker_update_monitor::notifications::email::{
    build_html, build_message, build_mismatch_section, build_plain, build_warnings_section, notify,
};
use lettre::transport::stub::StubTransport;
use lettre::Transport;

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
        stack: "mystack".to_string(),
        image: "nginx".to_string(),
        current_tag: "latest".to_string(),
        pattern: r"^\d+\.\d+\.\d+$".to_string(),
        reason: "did not match current tag".to_string(),
    }
}

fn make_warning() -> ScanWarning {
    ScanWarning {
        container_name: "test-app".to_string(),
        image: "nginx".to_string(),
        level: "warning".to_string(),
        message: "Could not fetch tags".to_string(),
    }
}

fn smtp_config() -> Config {
    Config {
        smtp_host: "smtp.example.com".to_string(),
        smtp_port: 587,
        smtp_from: "from@example.com".to_string(),
        smtp_to: "to@example.com".to_string(),
        smtp_username: String::new(),
        smtp_password: String::new(),
        smtp_tls: false,
        notify_endpoint: None,
        notify_auth_type: String::new(),
        notify_auth_token: String::new(),
        dry_run: false,
        notify_channels: "email".to_string(),
        cron_schedule: "0 * * * *".to_string(),
        label_prefix: "docker-update-monitor".to_string(),
        dockerhub_username: String::new(),
        dockerhub_password: String::new(),
        github_token: String::new(),
        run_on_startup: true,
        state_db_path: "/tmp/state.db".to_string(),
        log_level: "INFO".to_string(),
        web_port: 8080,
        dashboard_datetime_format: "%d/%m/%Y %H:%M".to_string(),
        tz: String::new(),
        update_cooldown: "0".to_string(),
    }
}

// --- build_html tests ---

#[test]
fn test_build_html_contains_update_info() {
    let html = build_html(&[make_update("new")], &[], &[]);
    assert!(html.contains("<table"), "expected <table in html");
    assert!(html.contains("nginx"));
    assert!(html.contains("1.0.0"));
    assert!(html.contains("1.1.0"));
    assert!(html.contains("mystack"));
}

#[test]
fn test_build_html_groups_by_status() {
    let updates = vec![make_update("new"), {
        let mut u = make_update("known");
        u.image = "redis".to_string();
        u
    }];
    let html = build_html(&updates, &[], &[]);
    assert!(html.contains("New updates"));
    assert!(html.contains("Known updates"));
    assert!(html.contains("nginx"));
    assert!(html.contains("redis"));
}

#[test]
fn test_build_html_includes_mismatch_section() {
    let html = build_html(&[make_update("new")], &[make_mismatch()], &[]);
    assert!(html.contains("Regex mismatches"));
    assert!(html.contains("latest"));
}

#[test]
fn test_build_html_includes_warnings_section() {
    let html = build_html(&[make_update("new")], &[], &[make_warning()]);
    assert!(html.contains("Warnings"));
    assert!(html.contains("Could not fetch tags"));
}

// --- build_plain tests ---

#[test]
fn test_build_plain_contains_update_info() {
    let text = build_plain(&[make_update("new")], &[], &[]);
    assert!(text.contains("nginx"));
    assert!(text.contains("1.0.0"));
    assert!(text.contains("1.1.0"));
    assert!(text.contains("mystack"));
}

#[test]
fn test_build_plain_groups_by_status() {
    let updates = vec![make_update("new"), {
        let mut u = make_update("known");
        u.stack = "stack-b".to_string();
        u
    }];
    let text = build_plain(&updates, &[], &[]);
    assert!(text.contains("New updates"));
    assert!(text.contains("Known updates"));
}

#[test]
fn test_build_plain_includes_mismatches() {
    let text = build_plain(&[make_update("new")], &[make_mismatch()], &[]);
    assert!(text.contains("Regex mismatches"));
    assert!(text.contains("pattern="));
}

#[test]
fn test_build_plain_includes_warnings() {
    let text = build_plain(&[make_update("new")], &[], &[make_warning()]);
    assert!(text.contains("Warnings"));
    assert!(text.contains("Could not fetch tags"));
}

// --- build_mismatch_section / build_warnings_section tests ---

#[test]
fn test_build_mismatch_section_empty_returns_empty() {
    assert_eq!(build_mismatch_section(&[]), "");
}

#[test]
fn test_build_warnings_section_empty_returns_empty() {
    assert_eq!(build_warnings_section(&[]), "");
}

// --- build_message tests ---

#[test]
fn test_build_message_singular_subject() {
    let config = smtp_config();
    let msg = build_message(&[make_update("new")], &[], &[], &config).unwrap();
    // Collapse folded header lines before checking
    let raw = String::from_utf8(msg.formatted())
        .unwrap()
        .replace("\r\n ", " ");
    // Emoji and en-dash are base64-encoded; "1 image update" appears literally
    assert!(
        raw.contains("1 image update"),
        "subject missing '1 image update': {raw}"
    );
}

#[test]
fn test_build_message_plural_subject() {
    let config = smtp_config();
    let updates: Vec<_> = (0..3).map(|_| make_update("new")).collect();
    let msg = build_message(&updates, &[], &[], &config).unwrap();
    let raw = String::from_utf8(msg.formatted())
        .unwrap()
        .replace("\r\n ", " ");
    assert!(
        raw.contains("3 image updates"),
        "subject missing '3 image updates': {raw}"
    );
}

#[test]
fn test_build_message_has_html_and_plain_parts() {
    let config = smtp_config();
    let msg = build_message(&[make_update("new")], &[], &[], &config).unwrap();
    let raw = String::from_utf8(msg.formatted()).unwrap();
    assert!(raw.contains("text/plain"));
    assert!(raw.contains("text/html"));
}

// --- notify (with stub transport) ---

#[test]
fn test_stub_transport_send_succeeds() {
    let config = smtp_config();
    let msg = build_message(&[make_update("new")], &[], &[], &config).unwrap();
    let stub = StubTransport::new_ok();
    stub.send(&msg).unwrap();
}

#[test]
fn test_missing_smtp_config_returns_ok() {
    let mut config = smtp_config();
    config.smtp_host = String::new();
    // notify should log warning and return Ok, not Err
    let result = notify(&[make_update("new")], &[], &[], &config);
    assert!(result.is_ok());
}

#[test]
fn test_empty_updates_returns_ok_without_sending() {
    let mut config = smtp_config();
    config.smtp_host = String::new(); // would fail if it tried to send
    let result = notify(&[], &[], &[], &config);
    assert!(result.is_ok());
}
