use rusqlite::{Connection, Result};

pub fn run_migrations(conn: &Connection) -> Result<()> {
    let mut stmt = conn.prepare("PRAGMA table_info(updates)")?;
    let existing_cols: std::collections::HashSet<String> = stmt
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<_>>()?;
    drop(stmt);

    if !existing_cols.contains("service_name") {
        conn.execute_batch("ALTER TABLE updates ADD COLUMN service_name TEXT NOT NULL DEFAULT ''")?;
    }

    if !existing_cols.contains("stack") {
        conn.execute_batch("ALTER TABLE updates ADD COLUMN stack TEXT NOT NULL DEFAULT ''")?;
    }

    // Create digests table if a pre-digests database is being migrated.
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS digests (
            image TEXT NOT NULL,
            tag TEXT NOT NULL,
            digest TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (image, tag)
        )",
    )?;

    // Replace UNIQUE(new_version) with UNIQUE(current_version) so repeated digest
    // changes for the same rolling tag overwrite rather than accumulate rows.
    if unique_index_has_new_version(conn)? {
        conn.execute_batch(
            "
            BEGIN;

            CREATE TABLE updates_new (
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
            );

            INSERT INTO updates_new
                (id, container_name, service_name, image, current_version,
                 new_version, update_type, stack, first_seen_at, last_seen_at,
                 notified_at, resolved_at)
            SELECT id, container_name, service_name, image, current_version,
                   new_version, update_type, stack, first_seen_at, last_seen_at,
                   notified_at, resolved_at
            FROM updates
            WHERE id IN (
                SELECT MAX(id)
                FROM updates
                GROUP BY container_name, image, current_version, update_type
            );

            DROP TABLE updates;
            ALTER TABLE updates_new RENAME TO updates;

            COMMIT;
        ",
        )?;
    }

    Ok(())
}

fn unique_index_has_new_version(conn: &Connection) -> Result<bool> {
    let mut idx_stmt = conn.prepare("PRAGMA index_list(updates)")?;
    let indices: Vec<(bool, String)> = idx_stmt
        .query_map([], |row| {
            let is_unique: bool = row.get::<_, i64>(2)? != 0;
            let name: String = row.get(1)?;
            Ok((is_unique, name))
        })?
        .collect::<Result<_>>()?;

    for (is_unique, idx_name) in indices {
        if !is_unique {
            continue;
        }
        let mut col_stmt = conn.prepare(&format!("PRAGMA index_info({idx_name})"))?;
        let cols: Vec<String> = col_stmt
            .query_map([], |row| row.get::<_, String>(2))?
            .collect::<Result<_>>()?;
        if cols.iter().any(|c| c == "new_version") {
            return Ok(true);
        }
    }
    Ok(false)
}
