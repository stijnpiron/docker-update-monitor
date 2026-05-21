use std::collections::{HashMap, HashSet};
use std::path::Path;

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension};

use crate::migrations::run_migrations;
use crate::models::UpdateInfo;
use crate::version::parse_tag;

// (container_name, image) → (current_tag, pattern)
pub type CurrentVersions = HashMap<(String, String), (String, String)>;
// (container_name, image) → list of RepoDigests strings
pub type RunningDigests = HashMap<(String, String), Vec<String>>;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    service_name TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL,
    current_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    update_type TEXT NOT NULL,
    stack TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at TEXT,
    resolved_at TEXT,
    UNIQUE(container_name, image, current_version, update_type)
)";

const DIGESTS_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS digests (
    image TEXT NOT NULL,
    tag TEXT NOT NULL,
    digest TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (image, tag)
)";

const METADATA_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)";

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct UpdateRecord {
    pub id: i64,
    pub container_name: String,
    pub service_name: String,
    pub image: String,
    pub current_version: String,
    pub new_version: String,
    pub update_type: String,
    pub stack: String,
    pub first_seen_at: String,
    pub last_seen_at: String,
    pub notified_at: Option<String>,
    pub resolved_at: Option<String>,
    pub status: String,
}

struct ActiveRow {
    id: i64,
    container_name: String,
    image: String,
    current_version: String,
    update_type: String,
    new_version: String,
}

pub fn open_db(path: &Path) -> anyhow::Result<Connection> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let conn = Connection::open(path)?;
    conn.execute_batch("PRAGMA journal_mode=WAL")?;
    conn.execute_batch(SCHEMA)?;
    conn.execute_batch(DIGESTS_SCHEMA)?;
    conn.execute_batch(METADATA_SCHEMA)?;
    run_migrations(&conn)?;
    Ok(conn)
}

pub fn process_scan(
    conn: &Connection,
    results: &[UpdateInfo],
    scan_time: Option<DateTime<Utc>>,
    current_versions: Option<&CurrentVersions>,
    running_digests: Option<&RunningDigests>,
) -> anyhow::Result<Vec<UpdateInfo>> {
    let ts = scan_time.unwrap_or_else(Utc::now).to_rfc3339();

    conn.execute_batch("BEGIN")?;

    let inner = || -> anyhow::Result<Vec<UpdateInfo>> {
        const UPSERT: &str = "
            INSERT INTO updates
                (container_name, service_name, image, current_version, new_version,
                 update_type, stack, first_seen_at, last_seen_at, resolved_at)
            VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, NULL)
            ON CONFLICT(container_name, image, current_version, update_type) DO UPDATE SET
                new_version    = excluded.new_version,
                last_seen_at   = excluded.last_seen_at,
                service_name   = excluded.service_name,
                stack          = excluded.stack,
                resolved_at    = NULL,
                notified_at    = CASE WHEN excluded.new_version != updates.new_version
                                      THEN NULL ELSE updates.notified_at END,
                first_seen_at  = CASE WHEN excluded.new_version != updates.new_version
                                      THEN excluded.first_seen_at ELSE updates.first_seen_at END";

        for u in results {
            conn.execute(
                UPSERT,
                rusqlite::params![
                    u.container_name,
                    u.service_name,
                    u.image,
                    u.current_version,
                    u.new_version,
                    u.update_type,
                    u.stack,
                    &ts,
                    &ts
                ],
            )?;
        }

        let current_keys: HashSet<(String, String, String, String)> = results
            .iter()
            .map(|u| {
                (
                    u.container_name.clone(),
                    u.image.clone(),
                    u.current_version.clone(),
                    u.update_type.clone(),
                )
            })
            .collect();

        // Collect active rows into a Vec to release the statement borrow before
        // executing DELETE / UPDATE statements below.
        let mut ar_stmt = conn.prepare(
            "SELECT id, container_name, image, current_version, update_type, new_version
             FROM updates WHERE resolved_at IS NULL",
        )?;
        let active_rows: Vec<ActiveRow> = ar_stmt
            .query_map([], |row| {
                Ok(ActiveRow {
                    id: row.get(0)?,
                    container_name: row.get(1)?,
                    image: row.get(2)?,
                    current_version: row.get(3)?,
                    update_type: row.get(4)?,
                    new_version: row.get(5)?,
                })
            })?
            .collect::<rusqlite::Result<_>>()?;
        drop(ar_stmt);

        let mut resolved_ids: Vec<i64> = Vec::new();

        for row in &active_rows {
            let key = (
                row.container_name.clone(),
                row.image.clone(),
                row.current_version.clone(),
                row.update_type.clone(),
            );
            if current_keys.contains(&key) {
                continue;
            }

            let cv_key = (row.container_name.clone(), row.image.clone());
            let Some(cv) = current_versions.and_then(|m| m.get(&cv_key)) else {
                continue;
            };
            let (current_tag, pattern) = cv;

            if row.update_type == "digest" {
                if current_tag != &row.current_version {
                    conn.execute("DELETE FROM updates WHERE id = ?1", [row.id])?;
                } else if let Some(rd) = running_digests {
                    let repo_digests = rd.get(&cv_key).map(Vec::as_slice).unwrap_or(&[]);
                    if !repo_digests.is_empty() {
                        let stored = conn
                            .query_row(
                                "SELECT digest FROM digests WHERE image = ?1 AND tag = ?2",
                                rusqlite::params![row.image, row.current_version],
                                |r| r.get::<_, String>(0),
                            )
                            .optional()?;

                        if let Some(stored_digest) = stored {
                            if repo_digests.iter().any(|rd| rd.contains(&stored_digest)) {
                                conn.execute(
                                    "UPDATE updates SET resolved_at = ?1 WHERE id = ?2",
                                    rusqlite::params![&ts, row.id],
                                )?;
                                resolved_ids.push(row.id);
                            }
                        }
                    }
                }
                continue;
            }

            let current_parsed = parse_tag(current_tag, pattern).ok().flatten();
            let new_parsed = parse_tag(&row.new_version, pattern).ok().flatten();
            let resolved =
                matches!((current_parsed, new_parsed), (Some(cur), Some(nv)) if cur >= nv);

            if resolved {
                conn.execute(
                    "UPDATE updates SET resolved_at = ?1 WHERE id = ?2",
                    rusqlite::params![&ts, row.id],
                )?;
                resolved_ids.push(row.id);
            } else {
                conn.execute("DELETE FROM updates WHERE id = ?1", [row.id])?;
            }
        }

        // Build result — active (non-resolved) rows
        let mut res_stmt = conn.prepare(
            "SELECT container_name, service_name, stack, image, current_version,
                    new_version, update_type, first_seen_at
             FROM updates WHERE resolved_at IS NULL ORDER BY first_seen_at",
        )?;
        let ts_ref = &ts;
        let mut result: Vec<UpdateInfo> = res_stmt
            .query_map([], |row| {
                let first_seen_at: String = row.get(7)?;
                let status = if &first_seen_at == ts_ref {
                    "new"
                } else {
                    "known"
                };
                Ok(UpdateInfo {
                    container_name: row.get(0)?,
                    service_name: row.get(1)?,
                    stack: row.get(2)?,
                    image: row.get(3)?,
                    current_version: row.get(4)?,
                    new_version: row.get(5)?,
                    update_type: row.get(6)?,
                    status: status.to_string(),
                    first_seen_at: Some(first_seen_at),
                })
            })?
            .collect::<rusqlite::Result<_>>()?;
        drop(res_stmt);

        // Resolved rows (those we just resolved in this scan)
        if !resolved_ids.is_empty() {
            let placeholders = resolved_ids
                .iter()
                .map(|_| "?")
                .collect::<Vec<_>>()
                .join(", ");
            let sql = format!(
                "SELECT container_name, service_name, stack, image, current_version,
                        new_version, update_type, first_seen_at
                 FROM updates WHERE id IN ({placeholders}) ORDER BY first_seen_at"
            );
            let mut stmt = conn.prepare(&sql)?;
            let resolved_rows = stmt
                .query_map(rusqlite::params_from_iter(resolved_ids.iter()), |row| {
                    Ok(UpdateInfo {
                        container_name: row.get(0)?,
                        service_name: row.get(1)?,
                        stack: row.get(2)?,
                        image: row.get(3)?,
                        current_version: row.get(4)?,
                        new_version: row.get(5)?,
                        update_type: row.get(6)?,
                        status: "resolved".to_string(),
                        first_seen_at: Some(row.get(7)?),
                    })
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            result.extend(resolved_rows);
        }

        Ok(result)
    };

    match inner() {
        Ok(v) => {
            conn.execute_batch("COMMIT")?;
            Ok(v)
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

pub fn get_all_updates(conn: &Connection) -> anyhow::Result<Vec<UpdateRecord>> {
    let mut stmt = conn.prepare(
        "SELECT id, container_name, service_name, image, current_version, new_version,
                update_type, stack, first_seen_at, last_seen_at, notified_at, resolved_at
         FROM updates ORDER BY first_seen_at",
    )?;
    let rows = stmt
        .query_map([], map_update_record)?
        .collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

pub fn get_active_updates(conn: &Connection) -> anyhow::Result<Vec<UpdateRecord>> {
    let mut stmt = conn.prepare(
        "SELECT id, container_name, service_name, image, current_version, new_version,
                update_type, stack, first_seen_at, last_seen_at, notified_at, resolved_at
         FROM updates WHERE resolved_at IS NULL ORDER BY first_seen_at",
    )?;
    let rows = stmt
        .query_map([], map_update_record)?
        .collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

fn map_update_record(row: &rusqlite::Row) -> rusqlite::Result<UpdateRecord> {
    let resolved_at: Option<String> = row.get(11)?;
    let notified_at: Option<String> = row.get(10)?;
    let status = if resolved_at.is_some() {
        "resolved"
    } else if notified_at.is_some() {
        "known"
    } else {
        "new"
    };
    Ok(UpdateRecord {
        id: row.get(0)?,
        container_name: row.get(1)?,
        service_name: row.get(2)?,
        image: row.get(3)?,
        current_version: row.get(4)?,
        new_version: row.get(5)?,
        update_type: row.get(6)?,
        stack: row.get(7)?,
        first_seen_at: row.get(8)?,
        last_seen_at: row.get(9)?,
        notified_at,
        resolved_at,
        status: status.to_string(),
    })
}

pub fn mark_notified(
    conn: &Connection,
    ids: &[i64],
    notified_time: Option<DateTime<Utc>>,
) -> anyhow::Result<()> {
    let ts = notified_time.unwrap_or_else(Utc::now).to_rfc3339();
    for &id in ids {
        conn.execute(
            "UPDATE updates SET notified_at = ?1 WHERE id = ?2 AND notified_at IS NULL",
            rusqlite::params![ts, id],
        )?;
    }
    Ok(())
}

pub fn save_last_check(conn: &Connection, ts: &DateTime<Utc>) -> anyhow::Result<()> {
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_check', ?1)",
        [ts.to_rfc3339()],
    )?;
    Ok(())
}

pub fn load_last_check(conn: &Connection) -> anyhow::Result<Option<DateTime<Utc>>> {
    let result = conn
        .query_row(
            "SELECT value FROM metadata WHERE key = 'last_check'",
            [],
            |row| row.get::<_, String>(0),
        )
        .optional()?;

    match result {
        Some(s) => {
            let dt = chrono::DateTime::parse_from_rfc3339(&s)
                .map_err(|e| anyhow::anyhow!("invalid last_check timestamp: {e}"))?
                .with_timezone(&Utc);
            Ok(Some(dt))
        }
        None => Ok(None),
    }
}

pub fn store_digest(
    conn: &Connection,
    image: &str,
    tag: &str,
    digest: &str,
    timestamp: Option<DateTime<Utc>>,
) -> anyhow::Result<()> {
    let ts = timestamp.unwrap_or_else(Utc::now).to_rfc3339();
    conn.execute(
        "INSERT OR REPLACE INTO digests (image, tag, digest, updated_at) VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![image, tag, digest, ts],
    )?;
    Ok(())
}

pub fn get_digest(conn: &Connection, image: &str, tag: &str) -> anyhow::Result<Option<String>> {
    let result = conn
        .query_row(
            "SELECT digest FROM digests WHERE image = ?1 AND tag = ?2",
            rusqlite::params![image, tag],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    Ok(result)
}
