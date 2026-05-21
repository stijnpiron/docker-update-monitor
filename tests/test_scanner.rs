use std::collections::HashMap;
use std::sync::Mutex;

use anyhow::Result;
use async_trait::async_trait;
use docker_update_monitor::{
    config::Config,
    health::HealthState,
    models::{RegexMismatch, ScanWarning, UpdateInfo},
    registry::manifest::Platform,
    scanner::{run_check, ContainerInfo, DockerClient, ScanOps},
    state::open_db,
};
use tempfile::NamedTempFile;

// ---------------------------------------------------------------------------
// Shared test helpers
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

fn make_container(name: &str, image_ref: &str, labels: HashMap<&str, &str>) -> ContainerInfo {
    ContainerInfo {
        name: name.to_string(),
        labels: labels
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect(),
        image_ref: image_ref.to_string(),
        repo_digests: Vec::new(),
        os: "linux".to_string(),
        arch: "amd64".to_string(),
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

pub struct MockOps {
    tags: HashMap<String, Vec<String>>,
    digests: HashMap<String, Option<String>>,
    manifest_lists: HashMap<String, Option<Vec<Platform>>>,
    dispatched: Mutex<Vec<UpdateInfo>>,
}

impl MockOps {
    fn new() -> Self {
        Self {
            tags: HashMap::new(),
            digests: HashMap::new(),
            manifest_lists: HashMap::new(),
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

    async fn fetch_manifest_list(&self, image: &str, tag: &str) -> Option<Vec<Platform>> {
        self.manifest_lists
            .get(&format!("{image}:{tag}"))
            .cloned()
            .flatten()
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
// Core tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_no_containers_no_updates() {
    let (_f, conn) = tmp_db();
    let docker = MockDocker { containers: vec![] };
    let ops = MockOps::new();
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    let dispatched = ops.dispatched();
    assert!(dispatched.is_empty());
}

#[tokio::test]
async fn test_container_without_tag_label_is_skipped() {
    let (_f, conn) = tmp_db();
    let container = make_container(
        "unlabeled",
        "nginx:latest",
        HashMap::new(), // no tag-regex label
    );
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new();
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    // No tags should have been fetched
    let dispatched = ops.dispatched();
    assert!(dispatched.is_empty());
}

#[tokio::test]
async fn test_updates_found_calls_notify() {
    let (_f, conn) = tmp_db();
    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.tag-regex", r"^(\d+)\.(\d+)\.(\d+)$");
    let container = make_container("outdated-app", "myimage:1.0.0", labels);
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new().with_tags("myimage", vec!["1.0.0", "2.0.0"]);
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    let dispatched = ops.dispatched();
    assert_eq!(dispatched.len(), 1);
    assert_eq!(dispatched[0].new_version, "2.0.0");
}

#[tokio::test]
async fn test_invalid_regex_skips_and_warns() {
    let (_f, conn) = tmp_db();
    let mut labels = HashMap::new();
    labels.insert("docker-update-monitor.tag-regex", r"^(\d+["); // invalid
    let container = make_container("bad-regex", "nginx:1.0.0", labels);
    let docker = MockDocker {
        containers: vec![container],
    };
    let ops = MockOps::new();
    let config = test_config();
    let health = HealthState::new();

    run_check(&docker, &ops, &conn, &config, &health)
        .await
        .unwrap();

    // No updates dispatched (container was skipped)
    let dispatched = ops.dispatched();
    assert!(dispatched.is_empty());
}
