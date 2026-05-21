fn default_cron_schedule() -> String {
    "0 * * * *".to_string()
}
fn default_label_prefix() -> String {
    "docker-update-monitor".to_string()
}
fn default_notify_channels() -> String {
    "webhook".to_string()
}
fn default_run_on_startup() -> bool {
    true
}
fn default_state_db_path() -> String {
    "/app/data/state.db".to_string()
}
fn default_log_level() -> String {
    "INFO".to_string()
}
fn default_smtp_port() -> u16 {
    587
}
fn default_smtp_tls() -> bool {
    true
}
fn default_web_port() -> u16 {
    8080
}
fn default_dashboard_datetime_format() -> String {
    "%d/%m/%Y %H:%M".to_string()
}
fn default_update_cooldown() -> String {
    "0".to_string()
}

#[derive(serde::Deserialize, Debug, Clone)]
pub struct Config {
    #[serde(default = "default_cron_schedule")]
    pub cron_schedule: String,
    #[serde(default = "default_label_prefix")]
    pub label_prefix: String,
    #[serde(default)]
    pub notify_endpoint: Option<String>,
    #[serde(default)]
    pub notify_auth_type: String,
    #[serde(default)]
    pub notify_auth_token: String,
    #[serde(default = "default_notify_channels")]
    pub notify_channels: String,
    #[serde(default)]
    pub dockerhub_username: String,
    #[serde(default)]
    pub dockerhub_password: String,
    #[serde(default)]
    pub github_token: String,
    #[serde(default = "default_run_on_startup")]
    pub run_on_startup: bool,
    #[serde(default)]
    pub dry_run: bool,
    #[serde(default = "default_state_db_path")]
    pub state_db_path: String,
    #[serde(default = "default_log_level")]
    pub log_level: String,
    #[serde(default)]
    pub smtp_host: String,
    #[serde(default = "default_smtp_port")]
    pub smtp_port: u16,
    #[serde(default)]
    pub smtp_username: String,
    #[serde(default)]
    pub smtp_password: String,
    #[serde(default)]
    pub smtp_from: String,
    #[serde(default)]
    pub smtp_to: String,
    #[serde(default = "default_smtp_tls")]
    pub smtp_tls: bool,
    #[serde(default = "default_web_port")]
    pub web_port: u16,
    #[serde(default = "default_dashboard_datetime_format")]
    pub dashboard_datetime_format: String,
    #[serde(default)]
    pub tz: String,
    #[serde(default = "default_update_cooldown")]
    pub update_cooldown: String,
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        envy::from_env::<Config>().map_err(Into::into)
    }
}
