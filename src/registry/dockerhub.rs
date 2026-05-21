use reqwest::Client;
use serde::Deserialize;

#[derive(Deserialize)]
struct LoginResponse {
    token: Option<String>,
}

#[derive(Deserialize)]
struct TagsPage {
    results: Vec<TagEntry>,
    next: Option<String>,
}

#[derive(Deserialize)]
struct TagEntry {
    name: String,
}

pub async fn get_dockerhub_token(
    client: &Client,
    username: &str,
    password: &str,
    login_url: &str,
) -> Option<String> {
    if username.is_empty() || password.is_empty() {
        return None;
    }
    let resp = client
        .post(login_url)
        .json(&serde_json::json!({"username": username, "password": password}))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: LoginResponse = resp.json().await.ok()?;
    data.token
}

pub async fn fetch_dockerhub_tags(
    client: &Client,
    image: &str,
    token: Option<&str>,
    current_tag: Option<&str>,
    api_base: &str,
) -> Vec<String> {
    let name = image.strip_prefix("docker.io/").unwrap_or(image);
    let parts: Vec<&str> = name.split('/').collect();
    let namespace = if parts.len() == 1 {
        "library"
    } else {
        parts[0]
    };
    let repo = parts[parts.len() - 1];

    let mut url = format!(
        "{api_base}/v2/namespaces/{namespace}/repositories/{repo}/tags?page_size=100&ordering=last_updated"
    );
    let mut tags = Vec::new();

    loop {
        let mut req = client.get(&url);
        if let Some(t) = token {
            req = req.header("Authorization", format!("Bearer {t}"));
        }
        let resp = match req.send().await {
            Ok(r) if r.status().is_success() => r,
            _ => break,
        };
        let page: TagsPage = match resp.json().await {
            Ok(p) => p,
            Err(_) => break,
        };
        let next_url = page.next.clone();
        let page_tags: Vec<String> = page.results.into_iter().map(|t| t.name).collect();
        let found = current_tag
            .map(|ct| page_tags.iter().any(|t| t == ct))
            .unwrap_or(false);
        tags.extend(page_tags);
        if found {
            break;
        }
        match next_url {
            Some(u) if !u.is_empty() => url = u,
            _ => break,
        }
    }

    tags
}
