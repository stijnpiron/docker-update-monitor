#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct UpdateInfo {
    pub container_name: String,
    pub service_name: String,
    pub stack: String,
    pub image: String,
    pub current_version: String,
    pub new_version: String,
    pub update_type: String,
    pub status: String,
    pub first_seen_at: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RegexMismatch {
    pub container_name: String,
    pub service_name: String,
    pub stack: String,
    pub image: String,
    pub current_tag: String,
    pub pattern: String,
    pub reason: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ScanWarning {
    pub container_name: String,
    pub image: String,
    pub level: String,
    pub message: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SkippedContainer {
    pub container_name: String,
    pub stack: String,
    pub image: String,
    pub reason: String,
}
