pub mod dockerhub;
pub mod ghcr;
pub mod manifest;

pub use manifest::Platform;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RegistryKind {
    DockerHub,
    Ghcr,
    Unknown,
}

pub fn detect_registry(image: &str) -> RegistryKind {
    if image.contains("://") {
        let host = image
            .split("://")
            .nth(1)
            .unwrap_or("")
            .split('/')
            .next()
            .unwrap_or("")
            .split(':')
            .next()
            .unwrap_or("")
            .to_lowercase();
        return match host.as_str() {
            "ghcr.io" | "lscr.io" => RegistryKind::Ghcr,
            "docker.io" => RegistryKind::DockerHub,
            _ => RegistryKind::Unknown,
        };
    }

    let first_segment = image.split('/').next().unwrap_or("").to_lowercase();
    let registry_host = first_segment.split(':').next().unwrap_or("");

    match registry_host {
        "ghcr.io" | "lscr.io" => return RegistryKind::Ghcr,
        "docker.io" => return RegistryKind::DockerHub,
        _ => {}
    }

    if !image.contains('/') {
        return RegistryKind::DockerHub;
    }

    let parts: Vec<&str> = image.split('/').collect();
    if parts.len() == 2 && !parts[0].contains('.') && !parts[0].contains(':') {
        return RegistryKind::DockerHub;
    }

    RegistryKind::Unknown
}

pub async fn fetch_all_tags(
    client: &reqwest::Client,
    image: &str,
    dockerhub_token: Option<&str>,
    github_token: &str,
    current_tag: Option<&str>,
) -> Vec<String> {
    match detect_registry(image) {
        RegistryKind::DockerHub => {
            dockerhub::fetch_dockerhub_tags(
                client,
                image,
                dockerhub_token,
                current_tag,
                "https://hub.docker.com",
            )
            .await
        }
        RegistryKind::Ghcr => {
            ghcr::fetch_ghcr_tags(
                client,
                image,
                github_token,
                current_tag,
                "https://api.github.com",
            )
            .await
        }
        RegistryKind::Unknown => vec![],
    }
}
