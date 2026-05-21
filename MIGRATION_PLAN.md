# Rust Migration Plan

## Strategy

Incremental strangler-fig migration with two deployable intermediate states:

1. **Milestone 1** — Rust scanner + Python dashboard (two containers, shared SQLite volume)
2. **Milestone 2** — Full Rust (single container, Python dropped)

The SQLite database is the natural seam. Scanner writes; dashboard reads. They can run as separate processes without code changes to the Python dashboard.

---

## Crate Inventory (`Cargo.toml`)

```toml
[package]
name = "docker-update-monitor"
version = "0.1.0"
edition = "2021"

[dependencies]
# Async runtime
tokio = { version = "1", features = ["full"] }

# HTTP client
reqwest = { version = "0.12", features = ["json", "rustls-tls"], default-features = false }

# HTTP server
axum = { version = "0.7", features = ["macros"] }
tower = "0.4"
tower-http = { version = "0.5", features = ["trace"] }

# Docker socket
bollard = "0.17"

# SQLite
rusqlite = { version = "0.32", features = ["bundled"] }

# Serialization
serde = { version = "1", features = ["derive"] }
serde_json = "1"

# Templating (dashboard HTML)
tera = "1"

# Prometheus metrics
prometheus = { version = "0.13", features = ["process"] }

# Email
lettre = { version = "0.11", features = ["tokio1", "tokio1-rustls-tls"], default-features = false }

# Cron scheduling
cron = "0.12"

# Regex
regex = "1"

# Environment / config
envy = "0.4"

# Date/time
chrono = { version = "0.4", features = ["serde"] }

# Logging
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter", "fmt"] }

# Retry logic
tower = { version = "0.4", features = ["retry", "timeout"] }

# Signal handling
tokio-signal = "0.3"   # or use tokio::signal directly

[dev-dependencies]
mockall = "0.13"
rstest = "0.23"
tokio-test = "0.4"
wiremock = "0.6"       # HTTP mock server for registry API tests
tempfile = "3"         # Temp SQLite DBs in tests
```

---

## Phase 0 — Scaffolding

**Goal:** Rust project compiles, CI runs, no functionality yet.

### Tasks

1. Create `rust/` subdirectory at repo root (keeps Python untouched during transition)
2. `rust/Cargo.toml` with full dependency list above
3. `rust/src/main.rs` — empty `fn main() {}`
4. `rust/src/lib.rs` — module declarations (add as phases complete)
5. Add `rust-toolchain.toml` pinning stable toolchain + targets:
   ```toml
   [toolchain]
   channel = "stable"
   targets = ["x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl", "armv7-unknown-linux-musleabihf"]
   ```
6. Add GitHub Actions workflow `.github/workflows/rust.yml`:
   - `cargo fmt --check`
   - `cargo clippy -- -D warnings`
   - `cargo test`
   - Triggers on PRs touching `rust/**`

### Done criteria
`cargo build` succeeds; CI workflow runs green on an empty test.

---

## Phase 1 — Core Types & Pure Logic

**Goal:** All pure Rust functions with tests matching Python test suite for the same modules.

### Files to create

#### `rust/src/models.rs`
```rust
// Mirrors app/models.py
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct UpdateInfo { ... }

#[derive(Debug, Clone)]
pub struct RegexMismatch { ... }

#[derive(Debug, Clone)]
pub struct ScanWarning { ... }
```

#### `rust/src/config.rs`
All env vars as a `Config` struct derived with `envy::from_env::<Config>()`.
Include defaults matching Python's `config.py`.

```rust
#[derive(serde::Deserialize, Debug, Clone)]
pub struct Config {
    #[serde(default = "default_cron")]
    pub cron_schedule: String,
    #[serde(default = "default_label_prefix")]
    pub label_prefix: String,
    pub notify_endpoint: Option<String>,
    // ... all env vars
}
```

#### `rust/src/version.rs`
Port `parse_tag()` and `find_updates()` from Python.
- `parse_tag(tag: &str, pattern: &str) -> anyhow::Result<Option<Vec<u64>>>` (capture groups as vec)
  - `Ok(Some(v))` — matched and all groups parsed as u64
  - `Ok(None)` — no match, or a group couldn't be parsed as u64
  - `Err(...)` — pattern has no capture groups (mirrors Python's `ValueError`)
- `find_updates(current: &[u64], candidates: &[(&str, Vec<u64>)]) -> Updates`

Key difference: Python uses `re.fullmatch()`; use `regex::Regex::new(format!("^(?:{})$", pattern))`.

#### `rust/src/cooldown.rs`
Port `parse_cooldown(s: &str) -> anyhow::Result<chrono::Duration>` from `cooldown.py`.
Returns `Err` on invalid format (mirrors Python's `ValueError`).

### Tests
Mirror Python tests directly:
- `tests/test_parse_tag.rs` → 6 parameterized tests with `rstest`
- `tests/test_find_updates.rs` → 4 tests
- `tests/test_parse_cooldown.rs` → 7 tests

### Done criteria
`cargo test` passes all 17 tests; results match Python test suite.

---

## Phase 2 — Infrastructure Layer

**Goal:** HTTP client with retries + SQLite state layer, fully tested.

### Files to create

#### `rust/src/http.rs`
Mirrors `app/http.py`.

```rust
pub fn build_client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .unwrap()
}
```

Retry logic: wrap with a `tower::retry::Retry` layer (3 attempts, retry on 429 and 5xx, respect `Retry-After` header).

#### `rust/src/migrations.rs`
Execute SQL migrations against a `rusqlite::Connection`.

```rust
pub fn run_migrations(conn: &Connection) -> rusqlite::Result<()>
```

One function per migration, guarded by `pragma user_version`.

#### `rust/src/state.rs`
Port all functions from `app/state.py`. Use `rusqlite::Connection` wrapped in `Arc<Mutex<Connection>>` for thread safety.

Key functions to implement:
```rust
pub fn process_scan(conn: &Connection, results: &[UpdateInfo]) -> Result<ScanResult>
pub fn get_all_updates(conn: &Connection) -> Result<Vec<UpdateRecord>>
pub fn mark_notified(conn: &Connection, ids: &[i64]) -> Result<()>
pub fn save_last_check(conn: &Connection, ts: &DateTime<Utc>) -> Result<()>
pub fn load_last_check(conn: &Connection) -> Result<Option<DateTime<Utc>>>
pub fn store_digest(conn: &Connection, image: &str, tag: &str, digest: &str) -> Result<()>
pub fn get_digest(conn: &Connection, image: &str, tag: &str) -> Result<Option<String>>
```

Test isolation: each test gets a `tempfile::NamedTempFile` as the DB path.

### Tests
- `tests/test_state.rs` — 11 unit tests + 3 integration tests
- `tests/test_migrations.rs` — 1 test verifying schema creation and idempotency

### Done criteria
All state tests pass; `store_digest` + `get_digest` round-trips work correctly; migration runs twice without error.

---

## Phase 3 — Registry Clients

**Goal:** DockerHub and GHCR tag fetching with HTTP mocked via `wiremock`.

### Files to create

#### `rust/src/registry/mod.rs`
Define the trait:
```rust
#[async_trait::async_trait]
pub trait RegistryClient: Send + Sync {
    async fn fetch_tags(&self, image: &str, current_tag: &str) -> Result<Vec<String>>;
    async fn fetch_digest(&self, image: &str, tag: &str) -> Result<String>;
}

pub fn detect_registry(image: &str) -> RegistryKind { ... }
pub async fn fetch_all_tags(image: &str, tag: &str, config: &Config) -> Result<Vec<String>>
```

#### `rust/src/registry/dockerhub.rs`
Port `app/registry/dockerhub.py`:
- Token auth via `hub.docker.com/v2/users/login`
- Paginated tag listing (100/page, early-stop on current tag)

#### `rust/src/registry/ghcr.rs`
Port `app/registry/ghcr.py`:
- GitHub Packages API with `GITHUB_TOKEN`
- Fallback org → user endpoint

#### `rust/src/registry/manifest.rs`
Port `app/registry/manifest.py`:
- Multi-arch manifest list parsing
- Platform-specific digest extraction

### Tests
Use `wiremock::MockServer` to intercept HTTP calls — no real registry calls in tests.
- `tests/test_registry_dockerhub.rs` — token fetch, tag pagination, early-stop
- `tests/test_registry_ghcr.rs` — version list, fallback behaviour
- `tests/test_registry_manifest.rs` — multi-arch manifest parsing
- `tests/test_detect_registry.rs` — 6 pattern tests (mirrors Python)

### Done criteria
All 7 registry tests pass using wiremock; no real HTTP calls in test suite.

---

## Phase 4 — Notifications

**Goal:** Webhook and SMTP email notifications.

### Files to create

#### `rust/src/notifications/mod.rs`
Dispatch based on `NOTIFY_CHANNELS` config (comma-separated).

```rust
pub async fn dispatch(updates: &[UpdateRecord], config: &Config) -> Result<()>
```

#### `rust/src/notifications/webhook.rs`
Port `app/notifications/webhook.py`:
- POST JSON to `NOTIFY_ENDPOINT`
- Auth: `None | Bearer | Basic`
- DRY_RUN mode: log only

#### `rust/src/notifications/email.rs`
Port `app/notifications/email.py` using `lettre`:
- SMTP with optional STARTTLS (port 587) or SMTPS (port 465)
- HTML + plain text multipart body

### Tests
- Webhook: mock HTTP server via `wiremock`
- Email: `lettre::transport::stub::StubTransport` (captures messages without sending)

### Done criteria
25 notification tests pass; DRY_RUN logs but doesn't send; stub transport used in all email tests.

---

## Phase 5 — Metrics & Health State

**Goal:** Prometheus metrics registration + in-memory health state.

### Files to create

#### `rust/src/metrics.rs`
Register all gauges and counters using `prometheus` crate's global registry.

```rust
lazy_static! {
    pub static ref CONTAINERS_MONITORED: Gauge = ...;
    pub static ref UPDATES_AVAILABLE: GaugeVec = ...;  // label: type
    pub static ref CHECK_DURATION: Gauge = ...;
    pub static ref LAST_CHECK_TIMESTAMP: Gauge = ...;
    pub static ref CHECK_ERRORS: Counter = ...;
    pub static ref NOTIFICATIONS_SENT: CounterVec = ...;  // label: channel
}

pub fn render_metrics() -> String  // prometheus text format
```

#### `rust/src/health.rs`
```rust
#[derive(Clone)]
pub struct HealthState {
    inner: Arc<RwLock<HealthStateInner>>,
}

struct HealthStateInner {
    last_check: Option<DateTime<Utc>>,
    next_check: Option<DateTime<Utc>>,
    containers_monitored: usize,
    warnings: Vec<ScanWarning>,
    skipped_containers: Vec<String>,
    started_at: DateTime<Utc>,
}

impl HealthState {
    pub fn update(&self, ...) { ... }
    pub fn to_json(&self) -> serde_json::Value { ... }
}
```

### Tests
- 4 metrics tests (mirrors Python)
- 2 health state tests

### Done criteria
Metrics render as valid Prometheus text format; concurrent reads of `HealthState` don't deadlock.

---

## Phase 6 — Scanner Core

**Goal:** The main scanning logic ported to Rust; tested with mock Docker client.

### Design note
`bollard` is async. Define a trait for the Docker client so tests can inject a mock:

```rust
#[async_trait::async_trait]
pub trait DockerClient: Send + Sync {
    async fn list_containers(&self) -> Result<Vec<ContainerSummary>>;
}

pub struct BollardClient(bollard::Docker);

#[async_trait::async_trait]
impl DockerClient for BollardClient {
    async fn list_containers(&self) -> Result<Vec<ContainerSummary>> { ... }
}
```

### Files to create

#### `rust/src/scanner.rs`
Port `app/scanner.py` (~511 lines).

```rust
pub async fn run_check(
    docker: &dyn DockerClient,
    conn: &Connection,
    config: &Config,
    health: &HealthState,
) -> Result<()>
```

Key logic to port:
- Label parsing (`docker-update-monitor.*` prefix)
- Mode detection (semver vs digest)
- Registry dispatch via `fetch_all_tags()`
- Version comparison via `find_updates()`
- Digest comparison via `store_digest()` / `get_digest()`
- Multi-arch platform check via `fetch_digest()` with platform filter
- Cooldown filtering
- Per-container label overrides (cooldown, regex pattern, mode)

### Tests
Use `mockall` to generate a mock `DockerClient`:

```rust
mock! {
    DockerClient {}
    #[async_trait]
    impl DockerClient for DockerClient {
        async fn list_containers(&self) -> Result<Vec<ContainerSummary>>;
    }
}
```

Mirror the Python test structure:
- `tests/test_scanner.rs` — 4 core tests
- `tests/test_digest_detection.rs` — 14 digest tests
- `tests/test_arch_check.rs` — 5 platform tests

### Done criteria
23 scanner tests pass; no real Docker socket used in tests.

---

## Phase 7 — HTTP Server (Axum)

**Goal:** All HTTP endpoints implemented and tested.

### Files to create

#### `rust/src/server.rs`

```rust
pub fn build_router(
    state: Arc<AppState>,
    scan_tx: tokio::sync::mpsc::Sender<()>,
) -> axum::Router
```

Routes:
| Method | Path | Handler |
|---|---|---|
| GET | `/` | `dashboard_handler` (Tera HTML) |
| GET | `/health` | `health_handler` (JSON) |
| GET | `/metrics` | `metrics_handler` (Prometheus text) |
| GET | `/api/updates` | `updates_handler` (JSON array) |
| GET | `/api/last-scan` | `last_scan_handler` (JSON) |
| POST | `/api/scan` | `scan_trigger_handler` (202 + send on channel) |

Manual scan trigger: `scan_tx` is a `tokio::sync::mpsc::Sender<()>`; main loop selects on the receiver.

#### `rust/templates/`
Port Jinja2 templates from `app/templates/` to Tera syntax. Tera is ~95% compatible with Jinja2; changes needed:
- `{% set x = ... %}` stays the same
- `loop.index` → `loop.index`
- Filter `|default` — same
- `{% if %}` — same

### Tests
Use `axum::test` utilities (no real TCP socket needed):

```rust
let app = build_router(state, scan_tx);
let response = app.oneshot(Request::get("/health")).await.unwrap();
assert_eq!(response.status(), 200);
```

Mirror Python dashboard tests: 11 route tests + XSS check.

### Done criteria
All 12 server tests pass; dashboard HTML renders without template errors; `/metrics` output parses as valid Prometheus format.

---

## Phase 8 — Main Loop & Scheduler

**Goal:** `main.rs` with Tokio runtime, cron loop, signal handling.

### `rust/src/main.rs`

```rust
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // 1. Init tracing (LOG_LEVEL env var)
    // 2. Load config via envy
    // 3. Open SQLite + run migrations
    // 4. Connect to Docker (bollard::Docker::connect_with_defaults())
    // 5. Init health state + metrics
    // 6. Create scan channel: (scan_tx, scan_rx)
    // 7. Spawn Axum server task
    // 8. Optionally run initial check (RUN_ON_STARTUP)
    // 9. Cron loop:
    //    loop {
    //      tokio::select! {
    //        _ = sleep_until(next_run) => run_check(...).await,
    //        _ = scan_rx.recv() => run_check(...).await,
    //        _ = signal::ctrl_c() => break,
    //      }
    //    }
}
```

Cron scheduling: use `cron` crate to parse `CRON_SCHEDULE` and compute next run time; sleep via `tokio::time::sleep_until`.

### Done criteria
Binary starts, logs to stdout, responds on `:8080`, handles `SIGTERM` gracefully, writes to `state.db`.

---

## Milestone 1 — Deploy: Rust Scanner + Python Dashboard

At this point the Rust binary replaces the Python scanner. The Python Flask dashboard continues to run unchanged, reading from the same SQLite file.

### Docker Compose changes

```yaml
services:
  scanner:
    build:
      context: .
      dockerfile: Dockerfile.rust
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./data:/app/data
    ports:
      - "8080:8080"       # /health, /metrics, /api/*
    environment: *common-env

  dashboard:
    build:
      context: .
      dockerfile: Dockerfile.python   # existing Dockerfile, dashboard-only entry
    volumes:
      - ./data:/app/data              # same SQLite volume
    ports:
      - "8081:8080"       # Flask dashboard on different host port
    command: ["python", "-m", "waitress", "--call", "app.dashboard:create_app"]
    environment: *common-env
```

### `Dockerfile.rust` (multi-stage)

```dockerfile
FROM rust:1.82-slim AS builder
WORKDIR /build
COPY rust/ .
RUN cargo build --release --target x86_64-unknown-linux-musl

FROM gcr.io/distroless/static:nonroot
COPY --from=builder /build/target/x86_64-unknown-linux-musl/release/docker-update-monitor /app/monitor
ENTRYPOINT ["/app/monitor"]
```

### Validation checklist
- [ ] `cargo test` passes (all phases)
- [ ] Docker image builds for amd64, arm64, armv7
- [ ] Scanner writes to `state.db` correctly
- [ ] `/health` returns 200 with valid JSON
- [ ] `/metrics` returns valid Prometheus text
- [ ] `POST /api/scan` triggers immediate scan
- [ ] Python dashboard reads updates from shared `state.db`
- [ ] Existing `state.db` from Python scanner is read correctly (schema compatibility)

---

## Phase 9 — Dashboard in Rust

**Goal:** Axum replaces the Flask dashboard. Tera templates replace Jinja2.

### Tasks

1. Finalize `rust/templates/index.html` (Tera port of Python Jinja2 templates)
2. Add static files if any (CSS/JS) via `tower-http::services::ServeDir`
3. Move `GET /` handler from stub to full Tera render
4. Add `DASHBOARD_DATETIME_FORMAT` config support in templates
5. Add `TZ` timezone conversion for display (use `chrono-tz` crate)

### Template migration notes

Python `dashboard.py` passes these to Jinja2:
- `updates` — list of `UpdateRecord` dicts (grouped by status)
- `skipped` — list of skipped container names
- `warnings` — list of warning strings
- `last_check`, `next_check` — formatted timestamps
- `datetime_format` — user's format string

All of these come from `HealthState` and `get_all_updates()` — already implemented.

---

## Milestone 2 — Full Rust

At this point Python is dropped entirely. One container, one binary.

### `Dockerfile` (replaces existing)

```dockerfile
FROM rust:1.82-slim AS builder
WORKDIR /build
COPY rust/ .
RUN cargo build --release

FROM gcr.io/distroless/static:nonroot
COPY --from=builder /build/target/release/docker-update-monitor /app/monitor
COPY --from=builder /build/templates/ /app/templates/
ENTRYPOINT ["/app/monitor"]
```

### CI/CD changes

| File | Change |
|---|---|
| `.github/workflows/tests.yml` | Replace pytest with `cargo test` |
| `.github/workflows/coverage.yml` | Replace pytest-cov with `cargo tarpaulin` |
| `.github/workflows/docker-image.yml` | Add `cross` for arm64/armv7 cross-compilation |
| `.github/workflows/rust.yml` | Promote from dev workflow to main CI |

### Python cleanup
- Remove `app/`, `tests/`, `requirements*.txt`, `pyproject.toml`, `Dockerfile` (Python)
- Move `rust/` contents to repo root
- Update `README.md`

### Final validation checklist
- [ ] Single container serves `/`, `/health`, `/metrics`, `/api/*`
- [ ] Image size < 25 MB
- [ ] All platforms build: amd64, arm64, armv7
- [ ] Existing `state.db` upgrades cleanly via migrations
- [ ] Full end-to-end: container with outdated image detected → webhook fired → dashboard shows update
- [ ] Graceful shutdown on SIGTERM
- [ ] Docker health check passes

---

## Testing Strategy Summary

| Phase | Python reference | Rust test count | Framework |
|---|---|---|---|
| 1 — Pure logic | 17 tests | 17 | `rstest` |
| 2 — State/SQLite | 15 tests | 15 | `tempfile` + `rusqlite` |
| 3 — Registry | 7 tests | 7 | `wiremock` |
| 4 — Notifications | 25 tests | 25 | `wiremock` + `lettre` stub |
| 5 — Metrics/Health | 6 tests | 6 | inline |
| 6 — Scanner | 23 tests | 23 | `mockall` |
| 7 — HTTP server | 12 tests | 12 | `axum::test` |
| 8 — Main/lifecycle | 7 tests | 7 | `tokio::test` |
| **Total** | **~112 core** | **~112** | |

Note: Python suite has 446 tests total; the higher count reflects extensive config-patching permutations. The Rust suite covers the same behaviour but consolidates combinatorial cases using `rstest` parameterization.

---

## Known Risks & Mitigations

| Risk | Mitigation |
|---|---|
| `bollard` API differences from Python Docker SDK | Use `DockerClient` trait from Phase 6; mock in tests; validate against real Docker socket in integration test |
| Regex behavior edge cases | Port all 6 `test_parse_tag` cases exactly; add property tests with `proptest` if edge cases found |
| Tera template differences from Jinja2 | Test every template variable in Phase 7 server tests; compare HTML output against Python reference |
| SQLite schema compatibility | Run Milestone 1 against an existing `state.db` as a mandatory validation step |
| `linux/arm/v7` cross-compilation | Set up `cross` crate early in Phase 0; validate in CI before Phase 8 |
| Compile times in CI | Use `sccache` + GitHub Actions cache for `~/.cargo` and `target/` |
