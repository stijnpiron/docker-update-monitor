use std::collections::HashMap;
use std::sync::Mutex;

use anyhow::Result;
use async_trait::async_trait;
use docker_update_monitor::{
    config::Config,
    health::HealthState,
    models::{RegexMismatch, ScanWarning, UpdateInfo},
    registry::manifest::Platform,
    scanner::{
        extract_local_digest, resolve_digest_to_tag, run_check, ContainerInfo, DockerClient,
        ScanOps,
    },
    state::{get_active_updates, get_digest, open_db, process_scan, store_digest},
};
use tempfile::NamedTempFile;

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

fn tmp_db() -> (NamedTempFile, rusqlite::Connection) {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    (f, conn)
}

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

fn make_container(
    name: &str,
    image_ref: &str,
    labels: HashMap<&str, &str>,
    repo_digests: Vec<&str>,
) -> ContainerInfo {
    ContainerInfo {
        name: name.to_string(),
        labels: labels
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect(),
        image_ref: image_ref.to_string(),
        repo_digests: repo_digests.iter().map(|s| s.to_string()).collect(),
        os: String::new(),
        arch: String::new(),
    }
}

struct MockDocker {
    containers: Vec<ContainerInfo>,
}

#[async_trait]
impl DockerClient for MockDocker {
    async fn list_containers(&self) -> Result<Vec<ContainerInfo>> {
        Ok(self.containers.clone())
    }
}

struct MockOps {
    tags: HashMap<String, Vec<String>>,
    digests: HashMap<String, Option<String>>,
    dispatched: Mutex<Vec<UpdateInfo>>,
}

impl MockOps {
    fn new() -> Self {
        Self {
            tags: HashMap::new(),
            digests: HashMap::new(),
            dispatched: Mutex::new(Vec::new()),
        }
    }

    fn with_tags(mut self, image: &str, tags: Vec<&str>) -> Self {
        self.tags.insert(
            image.to_string(),
            tags.into_iter().map(|s| s.to_string()).collect(),
        );
        self
    }

    fn with_digest(mut self, image_tag: &str, digest: Option<&str>) -> Self {
        self.digests
            .insert(image_tag.to_string(), digest.map(|s| s.to_string()));
        self
    }

    fn dispatched(&self) -> Vec<UpdateInfo> {
        self.dispatched.lock().unwrap().clone()
    }
}

#[async_trait]
impl ScanOps for MockOps {
    async fn fetch_all_tags(&self, image: &str, _current_tag: Option<&str>) -> Vec<String> {
        self.tags.get(image).cloned().unwrap_or_default()
    }

    async fn fetch_digest(&self, image: &str, tag: &str) -> Option<String> {
        self.digests
            .get(&format!("{image}:{tag}"))
            .cloned()
            .flatten()
    }

    async fn fetch_platform_digest(
        &self,
        _image: &str,
        _tag: &str,
        _os: &str,
        _arch: &str,
    ) -> Option<String> {
        None
    }

    async fn fetch_manifest_list(&self, _image: &str, _tag: &str) -> Option<Vec<Platform>> {
        None
    }

    async fn dispatch_notifications(
        &self,
        updates: &[UpdateInfo],
        _mismatches: &[RegexMismatch],
        _warnings: &[ScanWarning],
    ) -> Result<()> {
        *self.dispatched.lock().unwrap() = updates.to_vec();
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// extract_local_digest unit tests
// ---------------------------------------------------------------------------

#[test]
fn test_extract_standard_format() {
    assert_eq!(
        extract_local_digest(&["nginx@sha256:abc123".to_string()]),
        Some("sha256:abc123".to_string())
    );
}

#[test]
fn test_extract_registry_prefixed_format() {
    assert_eq!(
        extract_local_digest(&["ghcr.io/myorg/myimage@sha256:def456".to_string()]),
        Some("sha256:def456".to_string())
    );
}

#[test]
fn test_extract_multiple_entries_returns_first() {
    let digests = vec![
        "nginx@sha256:first111".to_string(),
        "docker.io/library/nginx@sha256:second222".to_string(),
    ];
    assert_eq!(
        extract_local_digest(&digests),
        Some("sha256:first111".to_string())
    );
}

#[test]
fn test_extract_empty_list() {
    assert_eq!(extract_local_digest(&[]), None);
}

#[test]
fn test_extract_no_at_sign() {
    assert_eq!(extract_local_digest(&["nginx:latest".to_string()]), None);
}

#[test]
fn test_extract_non_sha256_digest_skipped() {
    assert_eq!(extract_local_digest(&["nginx@md5:abc".to_string()]), None);
}

// ---------------------------------------------------------------------------
// resolve_digest_to_tag unit tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_resolve_finds_versioned_match() {
    let ops = MockOps::new()
        .with_digest("myimage:1.0.0", Some("sha256:other"))
        .with_digest("myimage:1.1.0", Some("sha256:other"))
        .with_digest("myimage:2.0.0", Some("sha256:target_digest"));

    let all_tags: Vec<String> = vec!["1.0.0", "1.1.0", "2.0.0", "latest"]
        .into_iter()
        .map(|s| s.to_string())
        .collect();

    let result = resolve_digest_to_tag(
        &ops,
        "myimage",
        "sha256:target_digest",
        &all_tags,
        r"^(\d+)\.(\d+)\.(\d+)$",
        "",
    )
    .await;

    assert_eq!(result, Some("2.0.0".to_string()));
}

#[tokio::test]
async fn test_resolve_no_match_returns_none() {
    let ops = MockOps::new()
        .with_digest("myimage:1.0.0", Some("sha256:different"))
        .with_digest("myimage:1.1.0", Some("sha256:different"));

    let all_tags: Vec<String> = vec!["1.0.0", "1.1.0"]
        .into_iter()
        .map(|s| s.to_string())
        .collect();

    let result = resolve_digest_to_tag(
        &ops,
        "myimage",
        "sha256:target_digest",
        &all_tags,
        r"^(\d+)\.(\d+)\.(\d+)$",
        "",
    )
    .await;

    assert_eq!(result, None);
}

#[tokio::test]
async fn test_resolve_finds_git_hash_tag() {
    let ops = MockOps::new().with_digest(
        "ghcr.io/myorg/myimage:sha-675e77e",
        Some("sha256:newdigest111"),
    );

    let all_tags: Vec<String> = vec!["edge", "sha-675e77e"]
        .into_iter()
        .map(|s| s.to_string())
        .collect();

    let result = resolve_digest_to_tag(
        &ops,
        "ghcr.io/myorg/myimage",
        "sha256:newdigest111",
        &all_tags,
        r"^(\d+)\.(\d+)\.(\d+)$",
        "edge",
    )
    .await;

    assert_eq!(result, Some("sha-675e77e".to_string()));
}

#[tokio::test]
async fn test_resolve_excludes_current_tag_from_fallback() {
    let ops = MockOps::new().with_digest("myimage:edge", Some("sha256:target"));

    let all_tags: Vec<String> = vec!["edge"].into_iter().map(|s| s.to_string()).collect();

    let result = resolve_digest_to_tag(
        &ops,
        "myimage",
        "sha256:target",
        &all_tags,
        r"^(\d+)\.(\d+)\.(\d+)$",
        "edge",
    )
    .await;

    assert_eq!(result, None);
}

// ---------------------------------------------------------------------------
// run_check integration tests — implicit digest mode
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_first_scan_stores_digest_no_update() {
    let (_f, conn) = tmp_db();
    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.tag-regex", r"^(\d+)\.(\d+)\.(\d+)$");
    let container = make_container("myapp", "myimage:latest", labels, vec![]);
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new()
        .with_tags("myimage", vec!["1.0.0", "1.1.0", "latest"])
        .with_digest("myimage:latest", Some("sha256:aabbcc1122334455"));
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    // Digest stored silently
    let stored = get_digest(&conn, "myimage", "latest").unwrap();
    assert_eq!(stored, Some("sha256:aabbcc1122334455".to_string()));

    // No digest update dispatched on first scan
    let dispatched = ops.dispatched();
    assert!(
        dispatched.iter().all(|u| u.update_type != "digest"),
        "First scan should not dispatch a digest update"
    );
}

#[tokio::test]
async fn test_digest_change_produces_update() {
    let (_f, conn) = tmp_db();
    // Pre-store old digest
    store_digest(
        &conn,
        "myimage",
        "latest",
        "sha256:olddigest000000000000",
        None,
    )
    .unwrap();

    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.tag-regex", r"^(\d+)\.(\d+)\.(\d+)$");
    let container = make_container("myapp", "myimage:latest", labels, vec![]);
    let docker = MockDocker {
        containers: vec![container],
    };
    // latest resolves to new digest; 1.2.0 shares that digest
    let ops = MockOps::new()
        .with_tags("myimage", vec!["1.0.0", "1.1.0", "1.2.0", "latest"])
        .with_digest("myimage:latest", Some("sha256:newdigest111111111111"))
        .with_digest("myimage:1.0.0", Some("sha256:other"))
        .with_digest("myimage:1.1.0", Some("sha256:other"))
        .with_digest("myimage:1.2.0", Some("sha256:newdigest111111111111"));
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    let dispatched = ops.dispatched();
    let digest_updates: Vec<_> = dispatched
        .iter()
        .filter(|u| u.update_type == "digest")
        .collect();
    assert_eq!(digest_updates.len(), 1);
    assert_eq!(digest_updates[0].current_version, "latest");
    assert_eq!(digest_updates[0].new_version, "1.2.0");
    assert_eq!(digest_updates[0].image, "myimage");
}

#[tokio::test]
async fn test_digest_unchanged_no_update() {
    let (_f, conn) = tmp_db();
    store_digest(
        &conn,
        "myimage",
        "latest",
        "sha256:samedigest000000000",
        None,
    )
    .unwrap();

    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.tag-regex", r"^(\d+)\.(\d+)\.(\d+)$");
    let container = make_container("myapp", "myimage:latest", labels, vec![]);
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new()
        .with_tags("myimage", vec!["1.0.0", "latest"])
        .with_digest("myimage:latest", Some("sha256:samedigest000000000"));
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    let dispatched = ops.dispatched();
    assert!(
        dispatched.iter().all(|u| u.update_type != "digest"),
        "Unchanged digest should not produce an update"
    );
}

#[tokio::test]
async fn test_digest_fetch_failure_produces_warning() {
    let (_f, conn) = tmp_db();

    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.tag-regex", r"^(\d+)\.(\d+)\.(\d+)$");
    let container = make_container("myapp", "myimage:latest", labels, vec![]);
    let docker = MockDocker {
        containers: vec![container],
    };
    // No digest configured → fetch_digest returns None
    let ops = MockOps::new().with_tags("myimage", vec!["1.0.0", "latest"]);
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    // Container skipped; no digest update
    let dispatched = ops.dispatched();
    assert!(dispatched.iter().all(|u| u.update_type != "digest"));
}

// ---------------------------------------------------------------------------
// mode=digest label tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_mode_digest_digests_match_no_update() {
    let (_f, conn) = tmp_db();

    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.mode", "digest");
    let container = make_container(
        "myapp",
        "myimage:latest",
        labels,
        vec!["myimage@sha256:local111"],
    );
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new().with_digest("myimage:latest", Some("sha256:local111"));
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    let dispatched = ops.dispatched();
    assert!(
        dispatched.iter().all(|u| u.update_type != "digest"),
        "Matching digests should not produce an update"
    );
}

#[tokio::test]
async fn test_mode_digest_digests_differ_reports_update() {
    let (_f, conn) = tmp_db();

    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.mode", "digest");
    let container = make_container(
        "myapp",
        "myimage:latest",
        labels,
        vec!["myimage@sha256:local111"],
    );
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new().with_digest("myimage:latest", Some("sha256:remote222"));
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    let dispatched = ops.dispatched();
    let digest_updates: Vec<_> = dispatched
        .iter()
        .filter(|u| u.update_type == "digest")
        .collect();
    assert_eq!(digest_updates.len(), 1);
    assert_eq!(digest_updates[0].current_version, "latest");
    assert_eq!(digest_updates[0].new_version, "sha256:remote222");
    assert_eq!(digest_updates[0].image, "myimage");
}

#[tokio::test]
async fn test_mode_digest_no_repo_digests_produces_warning() {
    let (_f, conn) = tmp_db();

    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.mode", "digest");
    // No RepoDigests
    let container = make_container("myapp", "myimage:latest", labels, vec![]);
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new().with_digest("myimage:latest", Some("sha256:remote222"));
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    // Should not produce a digest update (warning produced instead)
    let dispatched = ops.dispatched();
    assert!(dispatched.iter().all(|u| u.update_type != "digest"));
}

#[tokio::test]
async fn test_mode_digest_repulled_container_resolves_pending_update() {
    let (_f, conn) = tmp_db();

    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.mode", "digest");
    // Container now runs the updated image
    let container = make_container(
        "myapp",
        "myimage:latest",
        labels,
        vec!["myimage@sha256:remote222"],
    );
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new().with_digest("myimage:latest", Some("sha256:remote222"));

    // Pre-create a pending digest update in the DB
    store_digest(&conn, "myimage", "latest", "sha256:remote222", None).unwrap();
    process_scan(
        &conn,
        &[UpdateInfo {
            container_name: "myapp".to_string(),
            service_name: String::new(),
            stack: "standalone".to_string(),
            image: "myimage".to_string(),
            current_version: "latest".to_string(),
            new_version: "sha256:remote222".to_string(),
            update_type: "digest".to_string(),
            status: String::new(),
            first_seen_at: None,
        }],
        None,
        None,
        None,
    )
    .unwrap();
    assert_eq!(get_active_updates(&conn).unwrap().len(), 1);

    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    // Pending update should be resolved
    assert_eq!(get_active_updates(&conn).unwrap().len(), 0);
}
