pub mod email;
pub mod webhook;

use crate::config::Config;
use crate::models::{RegexMismatch, ScanWarning, UpdateInfo};
use anyhow::Result;

pub async fn dispatch(
    updates: &[UpdateInfo],
    mismatches: &[RegexMismatch],
    warnings: &[ScanWarning],
    client: &reqwest::Client,
    config: &Config,
) -> Result<()> {
    if updates.is_empty() && mismatches.is_empty() && warnings.is_empty() {
        return Ok(());
    }

    for channel in config.notify_channels.split(',').map(str::trim) {
        match channel {
            "webhook" => {
                webhook::notify(updates, mismatches, warnings, client, config).await?;
            }
            "email" => {
                email::notify(updates, mismatches, warnings, config)?;
            }
            other => {
                tracing::warn!("Unknown notification channel '{}' — skipping", other);
            }
        }
    }
    Ok(())
}
