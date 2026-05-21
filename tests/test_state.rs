use chrono::{TimeZone, Utc};
use docker_update_monitor::{
    models::UpdateInfo,
    state::{
        get_active_updates, get_all_updates, get_digest, load_last_check, mark_notified, open_db,
        process_scan, save_last_check, store_digest, CurrentVersions,
    },
};
use tempfile::NamedTempFile;

fn tmp_db() -> (NamedTempFile, rusqlite::Connection) {
    let f = NamedTempFile::new().unwrap();
    let conn = open_db(f.path()).unwrap();
    (f, conn)
}

fn make_update() -> UpdateInfo {
    UpdateInfo {
        container_name: "web".into(),
        service_name: "web".into(),
        stack: "mystack".into(),
        image: "nginx".into(),
        current_version: "1.0.0".into(),
        new_version: "1.1.0".into(),
        update_type: "minor".into(),
        status: String::new(),
        first_seen_at: None,
    }
}

// ── unit tests ────────────────────────────────────────────────────────────────

#[test]
fn test_open_db_creates_tables() {
    let (_f, conn) = tmp_db();
    // All three tables must exist after open_db
    for table in &["updates", "digests", "metadata"] {
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
                [table],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1, "table '{table}' missing");
    }
}

#[test]
fn test_process_scan_insert_new() {
    let (_f, conn) = tmp_db();
    let t = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    let result = process_scan(&conn, &[make_update()], Some(t), None, None).unwrap();
    assert_eq!(result.len(), 1);
    assert_eq!(result[0].status, "new");
    assert_eq!(result[0].container_name, "web");
}

#[test]
fn test_insert_sets_timestamps() {
    let (_f, conn) = tmp_db();
    let t = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    process_scan(&conn, &[make_update()], Some(t), None, None).unwrap();
    let rows = get_active_updates(&conn).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].first_seen_at, t.to_rfc3339());
    assert_eq!(rows[0].last_seen_at, t.to_rfc3339());
    assert!(rows[0].notified_at.is_none());
}

#[test]
fn test_upsert_is_known_on_second_scan() {
    let (_f, conn) = tmp_db();
    let t1 = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    let t2 = Utc.with_ymd_and_hms(2026, 1, 2, 0, 0, 0).unwrap();
    process_scan(&conn, &[make_update()], Some(t1), None, None).unwrap();
    let result = process_scan(&conn, &[make_update()], Some(t2), None, None).unwrap();
    assert_eq!(result.len(), 1);
    assert_eq!(result[0].status, "known");
    let rows = get_active_updates(&conn).unwrap();
    assert_eq!(rows[0].first_seen_at, t1.to_rfc3339());
    assert_eq!(rows[0].last_seen_at, t2.to_rfc3339());
}

#[test]
fn test_get_all_updates_empty() {
    let (_f, conn) = tmp_db();
    assert!(get_all_updates(&conn).unwrap().is_empty());
}

#[test]
fn test_get_all_updates_new() {
    let (_f, conn) = tmp_db();
    let t = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    process_scan(&conn, &[make_update()], Some(t), None, None).unwrap();
    let rows = get_all_updates(&conn).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].status, "new");
    assert_eq!(rows[0].container_name, "web");
}

#[test]
fn test_get_all_updates_known_after_notify() {
    let (_f, conn) = tmp_db();
    let t = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    process_scan(&conn, &[make_update()], Some(t), None, None).unwrap();
    let records = get_all_updates(&conn).unwrap();
    let ids: Vec<i64> = records.iter().map(|r| r.id).collect();
    mark_notified(&conn, &ids, Some(t)).unwrap();
    let rows = get_all_updates(&conn).unwrap();
    assert_eq!(rows[0].status, "known");
}

#[test]
fn test_mark_notified_sets_timestamp() {
    let (_f, conn) = tmp_db();
    let t = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    process_scan(&conn, &[make_update()], Some(t), None, None).unwrap();
    let ids: Vec<i64> = get_all_updates(&conn)
        .unwrap()
        .iter()
        .map(|r| r.id)
        .collect();
    mark_notified(&conn, &ids, Some(t)).unwrap();
    let rows = get_active_updates(&conn).unwrap();
    assert_eq!(rows[0].notified_at, Some(t.to_rfc3339()));
}

#[test]
fn test_mark_notified_idempotent() {
    let (_f, conn) = tmp_db();
    let t1 = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    let t2 = Utc.with_ymd_and_hms(2026, 1, 2, 0, 0, 0).unwrap();
    process_scan(&conn, &[make_update()], Some(t1), None, None).unwrap();
    let ids: Vec<i64> = get_all_updates(&conn)
        .unwrap()
        .iter()
        .map(|r| r.id)
        .collect();
    mark_notified(&conn, &ids, Some(t1)).unwrap();
    mark_notified(&conn, &ids, Some(t2)).unwrap(); // second call is a no-op
    let rows = get_active_updates(&conn).unwrap();
    assert_eq!(rows[0].notified_at, Some(t1.to_rfc3339()));
}

#[test]
fn test_store_get_digest() {
    let (_f, conn) = tmp_db();
    store_digest(&conn, "nginx", "latest", "sha256:abc123", None).unwrap();
    let got = get_digest(&conn, "nginx", "latest").unwrap();
    assert_eq!(got, Some("sha256:abc123".to_string()));
}

#[test]
fn test_get_digest_missing() {
    let (_f, conn) = tmp_db();
    let got = get_digest(&conn, "nginx", "latest").unwrap();
    assert!(got.is_none());
}

// ── integration tests ─────────────────────────────────────────────────────────

#[test]
fn test_resolve_when_container_updated() {
    let (_f, conn) = tmp_db();
    let t1 = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    let t2 = Utc.with_ymd_and_hms(2026, 1, 2, 0, 0, 0).unwrap();

    process_scan(&conn, &[make_update()], Some(t1), None, None).unwrap();

    let mut cv: CurrentVersions = CurrentVersions::new();
    cv.insert(
        ("web".into(), "nginx".into()),
        ("1.1.0".into(), r"^(\d+)\.(\d+)\.(\d+)$".into()),
    );
    let result = process_scan(&conn, &[], Some(t2), Some(&cv), None).unwrap();

    let resolved: Vec<_> = result.iter().filter(|r| r.status == "resolved").collect();
    assert_eq!(resolved.len(), 1);
    assert_eq!(resolved[0].new_version, "1.1.0");
    assert!(get_active_updates(&conn).unwrap().is_empty());
}

#[test]
fn test_delete_when_version_yanked() {
    let (_f, conn) = tmp_db();
    let t1 = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    let t2 = Utc.with_ymd_and_hms(2026, 1, 2, 0, 0, 0).unwrap();

    process_scan(&conn, &[make_update()], Some(t1), None, None).unwrap();

    // Container still at 1.0.0 but 1.1.0 was yanked — scanner reports empty
    let mut cv: CurrentVersions = CurrentVersions::new();
    cv.insert(
        ("web".into(), "nginx".into()),
        ("1.0.0".into(), r"^(\d+)\.(\d+)\.(\d+)$".into()),
    );
    let result = process_scan(&conn, &[], Some(t2), Some(&cv), None).unwrap();

    assert!(result.is_empty());
    assert!(get_all_updates(&conn).unwrap().is_empty());
}

#[test]
fn test_digest_dedup_second_replaces_first() {
    let (_f, conn) = tmp_db();
    let t1 = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
    let t2 = Utc.with_ymd_and_hms(2026, 1, 2, 0, 0, 0).unwrap();

    let u1 = UpdateInfo {
        container_name: "app".into(),
        service_name: "app".into(),
        stack: "mystack".into(),
        image: "ghcr.io/example/app".into(),
        current_version: "dev".into(),
        new_version: "sha-aaaaaa".into(),
        update_type: "digest".into(),
        status: String::new(),
        first_seen_at: None,
    };
    let u2 = UpdateInfo {
        new_version: "sha-bbbbbb".into(),
        ..u1.clone()
    };

    process_scan(&conn, &[u1], Some(t1), None, None).unwrap();
    process_scan(&conn, &[u2], Some(t2), None, None).unwrap();

    let active = get_active_updates(&conn).unwrap();
    assert_eq!(active.len(), 1);
    assert_eq!(active[0].new_version, "sha-bbbbbb");
}

// ── last-check round-trip ─────────────────────────────────────────────────────

#[test]
fn test_save_and_load_last_check() {
    let (_f, conn) = tmp_db();
    let t = Utc.with_ymd_and_hms(2026, 1, 1, 12, 0, 0).unwrap();
    assert!(load_last_check(&conn).unwrap().is_none());
    save_last_check(&conn, &t).unwrap();
    let got = load_last_check(&conn).unwrap().unwrap();
    assert_eq!(got, t);
}
