use crate::config::Config;
use crate::models::{RegexMismatch, ScanWarning, UpdateInfo};
use anyhow::Result;
use serde_json::{json, Map, Value};

pub fn build_payload(
    updates: &[UpdateInfo],
    mismatches: &[RegexMismatch],
    warnings: &[ScanWarning],
) -> Value {
    let mut grouped: Map<String, Value> = Map::new();

    for u in updates {
        let mut entry = serde_json::to_value(u).unwrap_or(Value::Null);
        if let Some(obj) = entry.as_object_mut() {
            obj.remove("status");
        }
        grouped
            .entry(u.status.clone())
            .or_insert_with(|| Value::Array(vec![]))
            .as_array_mut()
            .unwrap()
            .push(entry);
    }

    let mut payload: Map<String, Value> = Map::new();
    for (k, v) in grouped {
        if let Some(arr) = v.as_array() {
            if !arr.is_empty() {
                payload.insert(k, Value::Array(arr.clone()));
            }
        }
    }

    if !mismatches.is_empty() {
        payload.insert("regex_mismatches".to_string(), json!(mismatches));
    }
    if !warnings.is_empty() {
        payload.insert("warnings".to_string(), json!(warnings));
    }

    Value::Object(payload)
}

pub async fn notify(
    updates: &[UpdateInfo],
    mismatches: &[RegexMismatch],
    warnings: &[ScanWarning],
    client: &reqwest::Client,
    config: &Config,
) -> Result<()> {
    if updates.is_empty() && mismatches.is_empty() && warnings.is_empty() {
        return Ok(());
    }

    let payload = build_payload(updates, mismatches, warnings);

    if config.dry_run {
        tracing::info!(
            "DRY_RUN — would POST:\n{}",
            serde_json::to_string_pretty(&payload)?
        );
        return Ok(());
    }

    let endpoint = match &config.notify_endpoint {
        Some(ep) if !ep.is_empty() => ep.clone(),
        _ => {
            tracing::warn!("No NOTIFY_ENDPOINT set; skipping notification.");
            tracing::info!(
                "Updates found:\n{}",
                serde_json::to_string_pretty(&payload)?
            );
            return Ok(());
        }
    };

    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        reqwest::header::CONTENT_TYPE,
        "application/json".parse().unwrap(),
    );

    let auth_type = config.notify_auth_type.to_lowercase();
    if !auth_type.is_empty() && !config.notify_auth_token.is_empty() {
        match auth_type.as_str() {
            "bearer" => {
                let val = format!("Bearer {}", config.notify_auth_token);
                headers.insert(
                    reqwest::header::AUTHORIZATION,
                    val.parse().map_err(|e| anyhow::anyhow!("{e}"))?,
                );
            }
            "basic" => {
                let val = format!("Basic {}", config.notify_auth_token);
                headers.insert(
                    reqwest::header::AUTHORIZATION,
                    val.parse().map_err(|e| anyhow::anyhow!("{e}"))?,
                );
            }
            other => {
                tracing::warn!(
                    "Unknown NOTIFY_AUTH_TYPE '{}' — sending without authentication",
                    other
                );
            }
        }
    }

    match client
        .post(&endpoint)
        .headers(headers)
        .json(&payload)
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
    {
        Ok(resp) => {
            let status = resp.status();
            if let Err(e) = resp.error_for_status() {
                tracing::error!("Failed to notify endpoint: {}", e);
            } else {
                tracing::info!(
                    "Notified endpoint with {} update(s)  →  HTTP {}",
                    updates.len(),
                    status
                );
            }
        }
        Err(e) => {
            tracing::error!("Failed to notify endpoint: {}", e);
        }
    }

    Ok(())
}
