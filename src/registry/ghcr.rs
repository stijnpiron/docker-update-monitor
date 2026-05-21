use reqwest::{header::HeaderMap, Client};
use serde::Deserialize;

const MAX_PAGES: u32 = 100;

#[derive(Deserialize)]
struct VersionEntry {
    metadata: Option<MetadataEntry>,
}

#[derive(Deserialize)]
struct MetadataEntry {
    container: Option<ContainerEntry>,
}

#[derive(Deserialize)]
struct ContainerEntry {
    tags: Option<Vec<String>>,
}

pub async fn fetch_ghcr_tags(
    client: &Client,
    image: &str,
    github_token: &str,
    current_tag: Option<&str>,
    api_base: &str,
) -> Vec<String> {
    if github_token.is_empty() {
        return vec![];
    }

    let path = image
        .strip_prefix("ghcr.io/")
        .or_else(|| image.strip_prefix("lscr.io/"))
        .unwrap_or(image);
    let parts: Vec<&str> = path.split('/').collect();
    if parts.len() < 2 {
        return vec![];
    }
    let owner = parts[0];
    let repo = parts[1..].join("/");

    let mut headers = HeaderMap::new();
    headers.insert("Accept", "application/vnd.github+json".parse().unwrap());
    headers.insert(
        "Authorization",
        format!("Bearer {github_token}").parse().unwrap(),
    );
    headers.insert("X-GitHub-Api-Version", "2022-11-28".parse().unwrap());

    let mut tags = Vec::new();
    let mut page = 1u32;
    let mut base_url = format!("{api_base}/orgs/{owner}/packages/container/{repo}/versions");

    while page <= MAX_PAGES {
        let url = format!("{base_url}?per_page=100&page={page}");
        let mut resp_result = client.get(&url).headers(headers.clone()).send().await;

        // On first page, if org endpoint 404s, fall back to user endpoint
        if page == 1 {
            if let Ok(ref r) = resp_result {
                if r.status() == 404 {
                    base_url =
                        format!("{api_base}/users/{owner}/packages/container/{repo}/versions");
                    let url2 = format!("{base_url}?per_page=100&page={page}");
                    resp_result = client.get(&url2).headers(headers.clone()).send().await;
                }
            }
        }

        let resp = match resp_result {
            Ok(r) if r.status().is_success() => r,
            Ok(_) => break,
            Err(_) => break,
        };

        let versions: Vec<VersionEntry> = match resp.json().await {
            Ok(v) => v,
            Err(_) => break,
        };

        if versions.is_empty() {
            break;
        }

        let mut found_current = false;
        for version in versions {
            let version_tags: Vec<String> = version
                .metadata
                .and_then(|m| m.container)
                .and_then(|c| c.tags)
                .unwrap_or_default();
            if let Some(ct) = current_tag {
                if version_tags.iter().any(|t| t == ct) {
                    found_current = true;
                }
            }
            tags.extend(version_tags);
        }

        if found_current {
            break;
        }
        page += 1;
    }

    tags
}
