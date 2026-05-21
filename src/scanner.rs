use std::collections::HashMap;

use anyhow::Result;
use async_trait::async_trait;
use bollard::container::ListContainersOptions;
use bollard::Docker;
use chrono::Utc;
use lazy_static::lazy_static;
use regex::Regex;
use rusqlite::Connection;

use crate::config::Config;
use crate::cooldown::parse_cooldown;
use crate::health::HealthState;
use crate::metrics;
use crate::models::{RegexMismatch, ScanWarning, UpdateInfo};
use crate::registry::manifest::{
    fetch_digest, fetch_manifest_list, fetch_platform_digest, is_platform_supported, Platform,
};
use crate::state;
use crate::version::parse_tag;

lazy_static! {
    static ref GIT_HASH_RE: Regex = Regex::new(r"^sha-[a-f0-9]{7,}$").unwrap();
}

#[derive(Debug, Clone)]
pub struct ContainerInfo {
    pub name: String,
    pub labels: HashMap<String, String>,
    pub image_ref: String,
    pub repo_digests: Vec<String>,
    pub os: String,
    pub arch: String,
}

#[async_trait]
pub trait DockerClient: Send + Sync {
    async fn list_containers(&self) -> Result<Vec<ContainerInfo>>;
}

pub struct BollardClient {
    docker: Docker,
}

impl BollardClient {
    pub fn connect() -> Result<Self> {
        let docker = Docker::connect_with_local_defaults()?;
        Ok(Self { docker })
    }
}

#[async_trait]
impl DockerClient for BollardClient {
    async fn list_containers(&self) -> Result<Vec<ContainerInfo>> {
        let opts: ListContainersOptions<String> = ListContainersOptions {
            all: false,
            ..Default::default()
        };
        let summaries = self.docker.list_containers(Some(opts)).await?;
        let mut result = Vec::new();
        for s in summaries {
            let name = s
                .names
                .as_deref()
                .and_then(|n| n.first())
                .map(|n| n.trim_start_matches('/').to_string())
                .unwrap_or_default();
            let labels: HashMap<String, String> = s.labels.unwrap_or_default();
            let image_ref = s.image.unwrap_or_default();
            let image_id = s.image_id.unwrap_or_default();

            let (repo_digests, os, arch) = if !image_id.is_empty() {
                match self.docker.inspect_image(&image_id).await {
                    Ok(img) => {
                        let digests = img.repo_digests.unwrap_or_default();
                        let os = img.os.unwrap_or_default();
                        let arch = img.architecture.unwrap_or_default();
                        (digests, os, arch)
                    }
                    Err(_) => (Vec::new(), String::new(), String::new()),
                }
            } else {
                (Vec::new(), String::new(), String::new())
            };

            result.push(ContainerInfo {
                name,
                labels,
                image_ref,
                repo_digests,
                os,
                arch,
            });
        }
        Ok(result)
    }
}

#[async_trait]
pub trait ScanOps: Send + Sync {
    async fn fetch_all_tags(&self, image: &str, current_tag: Option<&str>) -> Vec<String>;
    async fn fetch_digest(&self, image: &str, tag: &str) -> Option<String>;
    async fn fetch_platform_digest(
        &self,
        image: &str,
        tag: &str,
        os: &str,
        arch: &str,
    ) -> Option<String>;
    async fn fetch_manifest_list(&self, image: &str, tag: &str) -> Option<Vec<Platform>>;
    async fn dispatch_notifications(
        &self,
        updates: &[UpdateInfo],
        mismatches: &[RegexMismatch],
        warnings: &[ScanWarning],
    ) -> Result<()>;
}

pub struct DefaultScanOps {
    client: reqwest::Client,
    config: Config,
}

impl DefaultScanOps {
    pub fn new(client: reqwest::Client, config: Config) -> Self {
        Self { client, config }
    }
}

#[async_trait]
impl ScanOps for DefaultScanOps {
    async fn fetch_all_tags(&self, image: &str, current_tag: Option<&str>) -> Vec<String> {
        let token = crate::registry::dockerhub::get_dockerhub_token(
            &self.client,
            &self.config.dockerhub_username,
            &self.config.dockerhub_password,
            "https://hub.docker.com/v2/users/login",
        )
        .await;
        crate::registry::fetch_all_tags(
            &self.client,
            image,
            token.as_deref(),
            &self.config.github_token,
            current_tag,
        )
        .await
    }

    async fn fetch_digest(&self, image: &str, tag: &str) -> Option<String> {
        fetch_digest(
            &self.client,
            image,
            tag,
            &self.config.dockerhub_username,
            &self.config.dockerhub_password,
            &self.config.github_token,
        )
        .await
    }

    async fn fetch_platform_digest(
        &self,
        image: &str,
        tag: &str,
        os: &str,
        arch: &str,
    ) -> Option<String> {
        fetch_platform_digest(
            &self.client,
            image,
            tag,
            os,
            arch,
            &self.config.dockerhub_username,
            &self.config.dockerhub_password,
            &self.config.github_token,
        )
        .await
    }

    async fn fetch_manifest_list(&self, image: &str, tag: &str) -> Option<Vec<Platform>> {
        fetch_manifest_list(
            &self.client,
            image,
            tag,
            &self.config.dockerhub_username,
            &self.config.dockerhub_password,
            &self.config.github_token,
        )
        .await
    }

    async fn dispatch_notifications(
        &self,
        updates: &[UpdateInfo],
        mismatches: &[RegexMismatch],
        warnings: &[ScanWarning],
    ) -> Result<()> {
        crate::notifications::dispatch(updates, mismatches, warnings, &self.client, &self.config)
            .await
    }
}

fn fullmatch(re: &Regex, text: &str) -> bool {
    re.find(text)
        .map(|m| m.start() == 0 && m.end() == text.len())
        .unwrap_or(false)
}

pub fn extract_local_digest(repo_digests: &[String]) -> Option<String> {
    for entry in repo_digests {
        if let Some((_, digest)) = entry.split_once('@') {
            if digest.starts_with("sha256:") {
                return Some(digest.to_string());
            }
        }
    }
    None
}

pub async fn resolve_digest_to_tag(
    ops: &dyn ScanOps,
    image: &str,
    target_digest: &str,
    all_tags: &[String],
    pattern: &str,
    current_tag: &str,
) -> Option<String> {
    let anchored = format!("^(?:{pattern})$");
    let pattern_re = Regex::new(&anchored).ok()?;

    let matching: Vec<&String> = all_tags
        .iter()
        .filter(|t| fullmatch(&pattern_re, t))
        .collect();
    for tag in matching {
        if ops.fetch_digest(image, tag).await.as_deref() == Some(target_digest) {
            return Some(tag.clone());
        }
    }

    let mut fallback: Vec<&String> = all_tags
        .iter()
        .filter(|t| *t != current_tag && !fullmatch(&pattern_re, t))
        .collect();
    fallback.sort_by_key(|t| if GIT_HASH_RE.is_match(t) { 0u8 } else { 1u8 });
    for tag in fallback.iter().take(20) {
        if ops.fetch_digest(image, tag).await.as_deref() == Some(target_digest) {
            return Some((*tag).clone());
        }
    }

    None
}

pub async fn run_check(
    docker: &dyn DockerClient,
    ops: &dyn ScanOps,
    conn: &Connection,
    config: &Config,
    health: &HealthState,
) -> Result<()> {
    let scan_start = std::time::Instant::now();

    tracing::info!("{}", "=".repeat(60));
    tracing::info!("Starting update check");
    tracing::info!("{}", "=".repeat(60));

    let containers = match docker.list_containers().await {
        Ok(c) => {
            health.set_docker_ok(true);
            c
        }
        Err(e) => {
            tracing::error!("Cannot connect to Docker: {}", e);
            health.set_docker_ok(false);
            metrics::CHECK_ERRORS.inc();
            return Ok(());
        }
    };

    if config.github_token.is_empty() {
        tracing::info!("No GITHUB_TOKEN set — ghcr.io images will be skipped.");
    } else {
        tracing::info!("GitHub token present — ghcr.io images will be checked.");
    }

    tracing::info!("Running containers: {}", containers.len());

    let mut tags_cache: HashMap<(String, String), Vec<String>> = HashMap::new();
    let mut all_updates: Vec<UpdateInfo> = Vec::new();
    let all_mismatches: Vec<RegexMismatch> = Vec::new();
    let mut all_warnings: Vec<ScanWarning> = Vec::new();
    let mut skipped_containers: Vec<crate::models::SkippedContainer> = Vec::new();
    let mut monitored_versions: state::CurrentVersions = HashMap::new();
    let mut running_digests: state::RunningDigests = HashMap::new();
    let mut container_cooldowns: HashMap<String, chrono::Duration> = HashMap::new();
    let mut monitored_count: usize = 0;

    for container in &containers {
        let container_name = &container.name;
        let labels = &container.labels;
        let mode = labels
            .get(&format!("{}.mode", config.label_prefix))
            .map(|s| s.to_lowercase())
            .unwrap_or_default();
        let pattern = labels.get(&format!("{}.tag-regex", config.label_prefix));

        if pattern.is_none() && mode != "digest" {
            tracing::debug!(
                "[{}] No '{}.tag-regex' label — skipping",
                container_name,
                config.label_prefix
            );
            let skip_stack = labels
                .get(&format!("{}.stack", config.label_prefix))
                .or_else(|| labels.get("com.docker.compose.project"))
                .cloned()
                .unwrap_or_else(|| "standalone".to_string());
            skipped_containers.push(crate::models::SkippedContainer {
                container_name: container_name.clone(),
                stack: skip_stack,
                image: container.image_ref.clone(),
                reason: format!("No '{}.tag-regex' label", config.label_prefix),
            });
            continue;
        }

        monitored_count += 1;

        let cooldown_str = labels
            .get(&format!("{}.update-cooldown", config.label_prefix))
            .map(|s| s.as_str())
            .unwrap_or(&config.update_cooldown);
        let cooldown = match parse_cooldown(cooldown_str) {
            Ok(d) => d,
            Err(_) => {
                tracing::warn!(
                    "[{}] Invalid update-cooldown value '{}' — using no cooldown",
                    container_name,
                    cooldown_str
                );
                chrono::Duration::zero()
            }
        };
        container_cooldowns.insert(container_name.clone(), cooldown);

        if let Some(pat) = pattern {
            if let Err(e) = Regex::new(pat) {
                let msg = format!("Invalid tag-regex '{pat}': {e}");
                tracing::warn!("[{}] {} — skipping", container_name, msg);
                all_warnings.push(ScanWarning {
                    container_name: container_name.clone(),
                    image: String::new(),
                    level: "warning".to_string(),
                    message: msg,
                });
                continue;
            }
        }

        let mut image_ref = container.image_ref.clone();
        if image_ref.is_empty() {
            let msg = "Cannot determine image reference".to_string();
            tracing::warn!("[{}] {} — skipping", container_name, msg);
            all_warnings.push(ScanWarning {
                container_name: container_name.clone(),
                image: String::new(),
                level: "warning".to_string(),
                message: msg,
            });
            continue;
        }

        if let Some(at) = image_ref.find('@') {
            image_ref.truncate(at);
        }

        let (image_name, current_tag) = {
            let last_part = image_ref.rsplit('/').next().unwrap_or(&image_ref);
            if last_part.contains(':') {
                let pos = image_ref.rfind(':').unwrap();
                (
                    image_ref[..pos].to_string(),
                    image_ref[pos + 1..].to_string(),
                )
            } else {
                (image_ref.clone(), "latest".to_string())
            }
        };

        let stack = labels
            .get(&format!("{}.stack", config.label_prefix))
            .or_else(|| labels.get("com.docker.compose.project"))
            .cloned()
            .unwrap_or_else(|| "standalone".to_string());
        let service_name = labels
            .get("com.docker.compose.service")
            .cloned()
            .unwrap_or_default();

        tracing::info!(
            "[{}]  image={}:{}  stack={}",
            container_name,
            image_name,
            current_tag,
            stack
        );

        if mode == "digest" {
            let local_digest = extract_local_digest(&container.repo_digests);

            if local_digest.is_none() {
                let msg = format!(
                    "No RepoDigests available for {}:{} — cannot compare",
                    image_name, current_tag
                );
                tracing::warn!("    {}", msg);
                all_warnings.push(ScanWarning {
                    container_name: container_name.clone(),
                    image: image_name.clone(),
                    level: "warning".to_string(),
                    message: msg,
                });
                monitored_versions.insert(
                    (container_name.clone(), image_name.clone()),
                    (current_tag.clone(), pattern.cloned().unwrap_or_default()),
                );
                running_digests.insert(
                    (container_name.clone(), image_name.clone()),
                    container.repo_digests.clone(),
                );
                continue;
            }
            let local_digest = local_digest.unwrap();

            let remote = ops.fetch_digest(&image_name, &current_tag).await;
            if remote.is_none() {
                let msg = format!(
                    "Could not fetch remote digest for {}:{}",
                    image_name, current_tag
                );
                tracing::warn!("    {}", msg);
                all_warnings.push(ScanWarning {
                    container_name: container_name.clone(),
                    image: image_name.clone(),
                    level: "warning".to_string(),
                    message: msg,
                });
                monitored_versions.insert(
                    (container_name.clone(), image_name.clone()),
                    (current_tag.clone(), pattern.cloned().unwrap_or_default()),
                );
                running_digests.insert(
                    (container_name.clone(), image_name.clone()),
                    container.repo_digests.clone(),
                );
                continue;
            }
            let remote_digest = remote.unwrap();

            if local_digest == remote_digest {
                tracing::info!(
                    "    Digest up to date ({}...)",
                    &local_digest[..local_digest.len().min(19)]
                );
            } else {
                let mut platform_match = false;
                if !container.os.is_empty() && !container.arch.is_empty() {
                    if let Some(pd) = ops
                        .fetch_platform_digest(
                            &image_name,
                            &current_tag,
                            &container.os,
                            &container.arch,
                        )
                        .await
                    {
                        if pd == local_digest {
                            platform_match = true;
                            tracing::info!(
                                "    Platform digest unchanged for {}/{} ({}...)",
                                container.os,
                                container.arch,
                                &local_digest[..local_digest.len().min(19)]
                            );
                        }
                    }
                }

                if !platform_match {
                    tracing::info!(
                        "    Digest changed: {}... → {}...",
                        &local_digest[..local_digest.len().min(19)],
                        &remote_digest[..remote_digest.len().min(19)]
                    );

                    let mut resolved_version = None;
                    if let Some(pat) = pattern {
                        let cache_key = (image_name.clone(), current_tag.clone());
                        if !tags_cache.contains_key(&cache_key) {
                            let tags = ops.fetch_all_tags(&image_name, Some(&current_tag)).await;
                            tags_cache.insert(cache_key.clone(), tags);
                        }
                        let all_tags = &tags_cache[&cache_key];
                        if !all_tags.is_empty() {
                            resolved_version = resolve_digest_to_tag(
                                ops,
                                &image_name,
                                &remote_digest,
                                all_tags,
                                pat,
                                &current_tag,
                            )
                            .await;
                        }
                    }
                    let new_version = resolved_version.unwrap_or_else(|| remote_digest.clone());
                    all_updates.push(UpdateInfo {
                        container_name: container_name.clone(),
                        service_name: service_name.clone(),
                        stack: stack.clone(),
                        image: image_name.clone(),
                        current_version: current_tag.clone(),
                        new_version,
                        update_type: "digest".to_string(),
                        status: String::new(),
                        first_seen_at: None,
                    });
                }
            }

            monitored_versions.insert(
                (container_name.clone(), image_name.clone()),
                (current_tag.clone(), pattern.cloned().unwrap_or_default()),
            );
            running_digests.insert(
                (container_name.clone(), image_name.clone()),
                container.repo_digests.clone(),
            );
            continue;
        }

        // Fetch tags
        let cache_key = (image_name.clone(), current_tag.clone());
        if !tags_cache.contains_key(&cache_key) {
            let tags = ops.fetch_all_tags(&image_name, Some(&current_tag)).await;
            tags_cache.insert(cache_key.clone(), tags);
        }
        let all_tags = &tags_cache[&cache_key];

        if all_tags.is_empty() {
            let msg = format!("No tags returned for {}", image_name);
            tracing::warn!("    {} — skipping", msg);
            all_warnings.push(ScanWarning {
                container_name: container_name.clone(),
                image: image_name.clone(),
                level: "warning".to_string(),
                message: msg,
            });
            continue;
        }

        // safe: we returned early if pattern.is_none() && mode != "digest"
        let pattern = pattern.unwrap();
        let anchored = format!("^(?:{pattern})$");
        let pattern_re = Regex::new(&anchored).unwrap(); // already validated above

        if !fullmatch(&pattern_re, &current_tag) {
            tracing::info!(
                "    Tag '{}' does not match pattern — using digest mode",
                current_tag
            );

            let current_digest = ops.fetch_digest(&image_name, &current_tag).await;
            if current_digest.is_none() {
                let msg = format!("Could not fetch digest for {}:{}", image_name, current_tag);
                tracing::warn!("    {} — skipping", msg);
                all_warnings.push(ScanWarning {
                    container_name: container_name.clone(),
                    image: image_name.clone(),
                    level: "warning".to_string(),
                    message: msg,
                });
                continue;
            }
            let current_digest = current_digest.unwrap();

            let stored = state::get_digest(conn, &image_name, &current_tag)?;

            match stored {
                None => {
                    tracing::info!(
                        "    First scan — storing digest {}...",
                        &current_digest[..current_digest.len().min(19)]
                    );
                    state::store_digest(conn, &image_name, &current_tag, &current_digest, None)?;
                }
                Some(ref stored_digest) if *stored_digest == current_digest => {
                    tracing::info!(
                        "    Digest unchanged ({}...)",
                        &current_digest[..current_digest.len().min(19)]
                    );
                }
                Some(stored_digest) => {
                    tracing::info!(
                        "    Digest changed: {}... → {}...",
                        &stored_digest[..stored_digest.len().min(19)],
                        &current_digest[..current_digest.len().min(19)]
                    );

                    let resolved = resolve_digest_to_tag(
                        ops,
                        &image_name,
                        &current_digest,
                        all_tags,
                        pattern,
                        &current_tag,
                    )
                    .await;

                    let new_version = if let Some(v) = resolved {
                        tracing::info!("    Resolved: {} → {}", current_tag, v);
                        v
                    } else {
                        tracing::info!(
                            "    Could not resolve to tag — using digest {}...",
                            &current_digest[..current_digest.len().min(19)]
                        );
                        current_digest.clone()
                    };

                    all_updates.push(UpdateInfo {
                        container_name: container_name.clone(),
                        service_name: service_name.clone(),
                        stack: stack.clone(),
                        image: image_name.clone(),
                        current_version: current_tag.clone(),
                        new_version,
                        update_type: "digest".to_string(),
                        status: String::new(),
                        first_seen_at: None,
                    });

                    state::store_digest(conn, &image_name, &current_tag, &current_digest, None)?;
                }
            }

            monitored_versions.insert(
                (container_name.clone(), image_name.clone()),
                (current_tag.clone(), pattern.to_string()),
            );
            running_digests.insert(
                (container_name.clone(), image_name.clone()),
                container.repo_digests.clone(),
            );
            continue;
        }

        // Semver mode: current tag matches pattern
        monitored_versions.insert(
            (container_name.clone(), image_name.clone()),
            (current_tag.clone(), pattern.to_string()),
        );

        let check_arch = labels
            .get(&format!("{}.check-arch", config.label_prefix))
            .map(|v| v.to_lowercase() != "false")
            .unwrap_or(true);

        let (container_os, container_arch) = if check_arch {
            if container.os.is_empty() || container.arch.is_empty() {
                tracing::warn!(
                    "[{}] Platform info unavailable from Docker API — skipping arch check",
                    container_name
                );
                (String::new(), String::new())
            } else {
                (container.os.clone(), container.arch.clone())
            }
        } else {
            (String::new(), String::new())
        };

        let current_parsed = parse_tag(&current_tag, pattern)
            .unwrap_or(None)
            .unwrap_or_default();
        let candidates: Vec<(&str, Vec<u64>)> = all_tags
            .iter()
            .filter_map(|t| {
                let parsed = parse_tag(t, pattern).ok()??;
                Some((t.as_str(), parsed))
            })
            .collect();
        let updates = crate::version::find_updates(&current_parsed, &candidates);

        if updates.is_empty() {
            tracing::info!("    No updates found (current={})", current_tag);
        } else {
            for (update_type, new_tag) in &updates {
                if check_arch && !container_os.is_empty() && !container_arch.is_empty() {
                    let platforms = ops.fetch_manifest_list(&image_name, new_tag).await;
                    if !is_platform_supported(platforms.as_deref(), &container_os, &container_arch)
                    {
                        tracing::info!(
                            "    Skipping {} update {} → {}: tag '{}' does not support {}/{}",
                            update_type.to_uppercase(),
                            current_tag,
                            new_tag,
                            new_tag,
                            container_os,
                            container_arch
                        );
                        continue;
                    }
                }

                tracing::info!(
                    "    {:5} update: {} → {}",
                    update_type.to_uppercase(),
                    current_tag,
                    new_tag
                );
                all_updates.push(UpdateInfo {
                    container_name: container_name.clone(),
                    service_name: service_name.clone(),
                    stack: stack.clone(),
                    image: image_name.clone(),
                    current_version: current_tag.clone(),
                    new_version: new_tag.clone(),
                    update_type: update_type.clone(),
                    status: String::new(),
                    first_seen_at: None,
                });
            }
        }
    }

    tracing::info!("{}", "-".repeat(60));
    tracing::info!("Check complete — {} update(s) detected", all_updates.len());

    let scan_time = Utc::now();
    let mut categorized = state::process_scan(
        conn,
        &all_updates,
        Some(scan_time),
        Some(&monitored_versions),
        Some(&running_digests),
    )?;

    // Deduplicate: keep highest new_version per (container, image, update_type)
    {
        let mut seen: HashMap<(String, String, String), String> = HashMap::new();
        let mut deduped: Vec<UpdateInfo> = Vec::new();
        for u in categorized {
            let key = (
                u.container_name.clone(),
                u.image.clone(),
                u.update_type.clone(),
            );
            let is_best = seen.get(&key).map(|v| &u.new_version > v).unwrap_or(true);
            if is_best {
                seen.insert(key, u.new_version.clone());
                deduped.push(u);
            }
        }
        categorized = deduped;
    }

    let new_count = categorized.iter().filter(|u| u.status == "new").count();
    let known_count = categorized.iter().filter(|u| u.status == "known").count();
    let resolved_count = categorized
        .iter()
        .filter(|u| u.status == "resolved")
        .count();
    tracing::info!(
        "  New: {}  |  Known: {}  |  Resolved: {}",
        new_count,
        known_count,
        resolved_count
    );

    let global_cooldown =
        parse_cooldown(&config.update_cooldown).unwrap_or_else(|_| chrono::Duration::zero());
    let mut actionable: Vec<UpdateInfo> = Vec::new();
    for u in &categorized {
        if u.status == "resolved" {
            actionable.push(u.clone());
            continue;
        }
        let cooldown = container_cooldowns
            .get(&u.container_name)
            .copied()
            .unwrap_or(global_cooldown);
        if !cooldown.is_zero() {
            if let Some(first_seen_str) = &u.first_seen_at {
                if let Ok(first_seen) = first_seen_str.parse::<chrono::DateTime<Utc>>() {
                    if scan_time - first_seen < cooldown {
                        tracing::info!(
                            "  [{}] {} → {} in cooldown, skipping notification",
                            u.container_name,
                            u.current_version,
                            u.new_version
                        );
                        continue;
                    }
                }
            }
        }
        actionable.push(u.clone());
    }

    ops.dispatch_notifications(&actionable, &all_mismatches, &all_warnings)
        .await?;

    if !actionable.is_empty() {
        let active = state::get_active_updates(conn)?;
        let actionable_ids: Vec<i64> = active
            .iter()
            .filter(|r| {
                actionable.iter().any(|u| {
                    u.container_name == r.container_name
                        && u.image == r.image
                        && u.update_type == r.update_type
                        && u.new_version == r.new_version
                })
            })
            .map(|r| r.id)
            .collect();
        if !actionable_ids.is_empty() {
            state::mark_notified(conn, &actionable_ids, Some(scan_time))?;
        }
    }

    if !all_warnings.is_empty() {
        metrics::CHECK_ERRORS.inc_by(all_warnings.len() as f64);
    }
    let all_db_updates = state::get_all_updates(conn)?;
    let updates_for_metrics: Vec<UpdateInfo> = all_db_updates
        .iter()
        .map(|r| UpdateInfo {
            container_name: r.container_name.clone(),
            service_name: r.service_name.clone(),
            stack: r.stack.clone(),
            image: r.image.clone(),
            current_version: r.current_version.clone(),
            new_version: r.new_version.clone(),
            update_type: r.update_type.clone(),
            status: r.status.clone(),
            first_seen_at: Some(r.first_seen_at.clone()),
        })
        .collect();
    metrics::update_after_scan(
        monitored_count as u64,
        &updates_for_metrics,
        scan_start.elapsed().as_secs_f64(),
        scan_time.timestamp() as f64,
    );

    if let Err(e) = state::save_last_check(conn, &scan_time) {
        tracing::warn!("Failed to persist last_check: {e}");
    }

    health.update(
        Some(scan_time),
        None,
        Some(monitored_count),
        Some(all_warnings),
        Some(skipped_containers),
    );

    Ok(())
}
