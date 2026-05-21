use reqwest::Client;
use serde::Deserialize;

use super::{detect_registry, RegistryKind};

#[derive(Debug, Clone, serde::Serialize, Deserialize)]
pub struct Platform {
    pub os: String,
    pub architecture: String,
    pub variant: Option<String>,
}

// Registry token response
#[derive(Deserialize)]
struct TokenResponse {
    token: Option<String>,
    access_token: Option<String>,
}

// Manifest list response (manifest.list.v2 or OCI image index)
#[derive(Deserialize)]
struct ManifestList {
    #[serde(rename = "mediaType", default)]
    media_type: String,
    #[serde(rename = "schemaVersion")]
    schema_version: Option<u32>,
    manifests: Option<Vec<ManifestEntry>>,
}

#[derive(Deserialize)]
struct ManifestEntry {
    platform: Option<PlatformRaw>,
    digest: Option<String>,
}

#[derive(Deserialize)]
struct PlatformRaw {
    os: Option<String>,
    architecture: Option<String>,
    variant: Option<String>,
}

async fn get_registry_token(
    client: &Client,
    token_url: &str,
    service: &str,
    scope: &str,
    bearer_token: &str,
    username: &str,
    password: &str,
) -> Option<String> {
    let mut req = client
        .get(token_url)
        .query(&[("service", service), ("scope", scope)]);

    if !bearer_token.is_empty() {
        req = req.bearer_auth(bearer_token);
    } else if !username.is_empty() && !password.is_empty() {
        req = req.basic_auth(username, Some(password));
    }

    let resp = req.send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: TokenResponse = resp.json().await.ok()?;
    data.token.or(data.access_token)
}

fn parse_manifest_platforms(data: ManifestList) -> Option<Vec<Platform>> {
    let is_list =
        data.media_type.contains("manifest.list") || data.media_type.contains("image.index");
    let is_schema_v2 = data.schema_version == Some(2);

    if !is_list && !is_schema_v2 {
        return None;
    }

    let entries = data.manifests?;
    let platforms: Vec<Platform> = entries
        .into_iter()
        .filter_map(|e| {
            let p = e.platform?;
            Some(Platform {
                os: p.os.unwrap_or_default(),
                architecture: p.architecture.unwrap_or_default(),
                variant: p.variant,
            })
        })
        .collect();

    if platforms.is_empty() {
        None
    } else {
        Some(platforms)
    }
}

async fn fetch_platforms_from_url(
    client: &Client,
    manifest_url: &str,
    auth_token: &str,
) -> Option<Vec<Platform>> {
    let accept = concat!(
        "application/vnd.docker.distribution.manifest.list.v2+json,",
        "application/vnd.oci.image.index.v1+json"
    );
    let resp = client
        .get(manifest_url)
        .header("Accept", accept)
        .bearer_auth(auth_token)
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: ManifestList = resp.json().await.ok()?;
    parse_manifest_platforms(data)
}

async fn fetch_digest_from_url(
    client: &Client,
    manifest_url: &str,
    auth_token: &str,
) -> Option<String> {
    let accept = concat!(
        "application/vnd.docker.distribution.manifest.v2+json,",
        "application/vnd.oci.image.manifest.v1+json,",
        "application/vnd.docker.distribution.manifest.list.v2+json,",
        "application/vnd.oci.image.index.v1+json"
    );
    let resp = client
        .head(manifest_url)
        .header("Accept", accept)
        .bearer_auth(auth_token)
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    resp.headers()
        .get("Docker-Content-Digest")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

async fn fetch_platform_digest_from_url(
    client: &Client,
    manifest_url: &str,
    auth_token: &str,
    os: &str,
    arch: &str,
) -> Option<String> {
    let accept = concat!(
        "application/vnd.docker.distribution.manifest.list.v2+json,",
        "application/vnd.oci.image.index.v1+json"
    );
    let resp = client
        .get(manifest_url)
        .header("Accept", accept)
        .bearer_auth(auth_token)
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: ManifestList = resp.json().await.ok()?;
    let entries = data.manifests?;
    for entry in entries {
        if let Some(p) = &entry.platform {
            if p.os.as_deref() == Some(os) && p.architecture.as_deref() == Some(arch) {
                return entry.digest;
            }
        }
    }
    None
}

// ---------------------------------------------------------------------------
// DockerHub helpers
// ---------------------------------------------------------------------------

fn dockerhub_image_name(image: &str) -> String {
    let name = image.strip_prefix("docker.io/").unwrap_or(image);
    let parts: Vec<&str> = name.split('/').collect();
    if parts.len() == 1 {
        format!("library/{name}")
    } else {
        name.to_string()
    }
}

async fn dockerhub_token(
    client: &Client,
    name: &str,
    username: &str,
    password: &str,
    auth_base: &str,
) -> Option<String> {
    let token_url = format!("{auth_base}/token");
    let scope = format!("repository:{name}:pull");
    get_registry_token(
        client,
        &token_url,
        "registry.docker.io",
        &scope,
        "",
        username,
        password,
    )
    .await
}

async fn ghcr_token(
    client: &Client,
    host: &str,
    path: &str,
    github_token: &str,
    registry_base: &str,
) -> Option<String> {
    if github_token.is_empty() {
        return None;
    }
    let token_url = format!("{registry_base}/token");
    let scope = format!("repository:{path}:pull");
    get_registry_token(client, &token_url, host, &scope, github_token, "", "").await
}

fn ghcr_host_and_path(image: &str) -> (String, String) {
    let image = image.trim();
    let parsed_host = if image.contains("://") {
        image
            .split("://")
            .nth(1)
            .unwrap_or("")
            .split('/')
            .next()
            .unwrap_or("")
            .split(':')
            .next()
            .unwrap_or("")
            .to_lowercase()
    } else if image.contains('/') {
        image.split('/').next().unwrap_or("").to_lowercase()
    } else {
        String::new()
    };

    let host = if parsed_host == "lscr.io" {
        "lscr.io".to_string()
    } else {
        "ghcr.io".to_string()
    };
    let path = image
        .strip_prefix(&format!("{host}/"))
        .unwrap_or(image)
        .to_string();
    (host, path)
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

pub fn is_platform_supported(platforms: Option<&[Platform]>, os: &str, arch: &str) -> bool {
    match platforms {
        None => true,
        Some(list) => list.iter().any(|p| p.os == os && p.architecture == arch),
    }
}

pub async fn fetch_manifest_list(
    client: &Client,
    image: &str,
    tag: &str,
    dh_username: &str,
    dh_password: &str,
    github_token: &str,
) -> Option<Vec<Platform>> {
    match detect_registry(image) {
        RegistryKind::DockerHub => {
            let name = dockerhub_image_name(image);
            let token = dockerhub_token(
                client,
                &name,
                dh_username,
                dh_password,
                "https://auth.docker.io",
            )
            .await?;
            let url = format!("https://registry-1.docker.io/v2/{name}/manifests/{tag}");
            fetch_platforms_from_url(client, &url, &token).await
        }
        RegistryKind::Ghcr => {
            let (host, path) = ghcr_host_and_path(image);
            let token = ghcr_token(
                client,
                &host,
                &path,
                github_token,
                &format!("https://{host}"),
            )
            .await?;
            let url = format!("https://{host}/v2/{path}/manifests/{tag}");
            fetch_platforms_from_url(client, &url, &token).await
        }
        RegistryKind::Unknown => None,
    }
}

pub async fn fetch_digest(
    client: &Client,
    image: &str,
    tag: &str,
    dh_username: &str,
    dh_password: &str,
    github_token: &str,
) -> Option<String> {
    match detect_registry(image) {
        RegistryKind::DockerHub => {
            let name = dockerhub_image_name(image);
            let token = dockerhub_token(
                client,
                &name,
                dh_username,
                dh_password,
                "https://auth.docker.io",
            )
            .await?;
            let url = format!("https://registry-1.docker.io/v2/{name}/manifests/{tag}");
            fetch_digest_from_url(client, &url, &token).await
        }
        RegistryKind::Ghcr => {
            let (host, path) = ghcr_host_and_path(image);
            let token = ghcr_token(
                client,
                &host,
                &path,
                github_token,
                &format!("https://{host}"),
            )
            .await?;
            let url = format!("https://{host}/v2/{path}/manifests/{tag}");
            fetch_digest_from_url(client, &url, &token).await
        }
        RegistryKind::Unknown => None,
    }
}

#[allow(clippy::too_many_arguments)]
pub async fn fetch_platform_digest(
    client: &Client,
    image: &str,
    tag: &str,
    os: &str,
    arch: &str,
    dh_username: &str,
    dh_password: &str,
    github_token: &str,
) -> Option<String> {
    match detect_registry(image) {
        RegistryKind::DockerHub => {
            let name = dockerhub_image_name(image);
            let token = dockerhub_token(
                client,
                &name,
                dh_username,
                dh_password,
                "https://auth.docker.io",
            )
            .await?;
            let url = format!("https://registry-1.docker.io/v2/{name}/manifests/{tag}");
            fetch_platform_digest_from_url(client, &url, &token, os, arch).await
        }
        RegistryKind::Ghcr => {
            let (host, path) = ghcr_host_and_path(image);
            let token = ghcr_token(
                client,
                &host,
                &path,
                github_token,
                &format!("https://{host}"),
            )
            .await?;
            let url = format!("https://{host}/v2/{path}/manifests/{tag}");
            fetch_platform_digest_from_url(client, &url, &token, os, arch).await
        }
        RegistryKind::Unknown => None,
    }
}
