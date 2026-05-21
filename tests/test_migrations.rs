use docker_update_monitor::migrations::run_migrations;
use rusqlite::Connection;

const OLD_SCHEMA: &str = "
CREATE TABLE updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    image TEXT NOT NULL,
    current_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    update_type TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at TEXT,
    resolved_at TEXT,
    UNIQUE(container_name, image, new_version, update_type)
)";

fn old_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(OLD_SCHEMA).unwrap();
    conn
}

/// Single combined test: verifies schema creation and idempotency in one pass.
/// Mirrors Python's TestMigrations class.
#[test]
fn test_run_migrations_schema_and_idempotency() {
    let conn = old_db();

    // First run: adds columns, creates digests table, rewrites UNIQUE constraint
    run_migrations(&conn).unwrap();

    // service_name and stack columns must exist
    let cols: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(updates)")
        .unwrap()
        .query_map([], |row| row.get::<_, String>(1))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect();
    assert!(cols.contains("service_name"), "service_name column missing");
    assert!(cols.contains("stack"), "stack column missing");

    // UNIQUE index must now be on current_version, not new_version
    let mut unique_cols: std::collections::HashSet<String> = Default::default();
    let idx_rows: Vec<(bool, String)> = conn
        .prepare("PRAGMA index_list(updates)")
        .unwrap()
        .query_map([], |row| {
            Ok((row.get::<_, i64>(2)? != 0, row.get::<_, String>(1)?))
        })
        .unwrap()
        .filter_map(|r| r.ok())
        .collect();
    for (is_unique, idx_name) in idx_rows {
        if !is_unique {
            continue;
        }
        conn.prepare(&format!("PRAGMA index_info({idx_name})"))
            .unwrap()
            .query_map([], |row| row.get::<_, String>(2))
            .unwrap()
            .filter_map(|r| r.ok())
            .for_each(|c| {
                unique_cols.insert(c);
            });
    }
    assert!(unique_cols.contains("current_version"));
    assert!(!unique_cols.contains("new_version"));

    // digests table must exist
    let digests_exists: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='digests'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(digests_exists, 1);

    // Idempotency: second run must succeed without error
    run_migrations(&conn).unwrap();

    // Schema unchanged after second run
    let cols2: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(updates)")
        .unwrap()
        .query_map([], |row| row.get::<_, String>(1))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect();
    assert!(cols2.contains("service_name"));
    assert!(cols2.contains("stack"));
}
