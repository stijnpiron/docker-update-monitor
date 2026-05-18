# Docker Update Monitor

![GitHub Release](https://img.shields.io/github/v/release/stijnpiron/docker-update-monitor)

![Build & Push Docker Image](https://github.com/stijnpiron/docker-update-monitor/actions/workflows/docker-image.yml/badge.svg)
![Tests](https://github.com/stijnpiron/docker-update-monitor/actions/workflows/tests.yml/badge.svg)
![Coverage](https://github.com/stijnpiron/docker-update-monitor/actions/workflows/coverage.yml/badge.svg)
![CodeQL](https://github.com/stijnpiron/docker-update-monitor/actions/workflows/codeql.yml/badge.svg)
![GitHub Issues](https://img.shields.io/github/issues/stijnpiron/docker-update-monitor)

Monitors all running Docker containers for image updates and POSTs a structured
payload to a webhook when updates are found.

## How it works

1. Reads all running containers from the Docker socket.
2. For each monitored container it picks a detection mode:

   **Semver mode** (container has `docker-update-monitor.tag-regex` and the current tag matches it)
   - Fetches all available tags from the registry.
   - Applies your regex to parse version numbers from each tag.
   - Finds the **best** (highest) update per level — one for patch, one for minor, one for major.
     You never receive intermediate versions, only the latest available at each level.

   **Implicit digest mode** (container has `tag-regex` but the current tag does _not_ match it — e.g. `:latest`, `:edge`)
   - Compares the registry manifest digest across scans.
   - Silent on the first scan; reports a `digest` update when the digest changes on a later scan.
   - Attempts to resolve the new digest to a versioned tag; falls back to the raw digest.

   **Explicit digest mode** (container has `docker-update-monitor.mode: digest`)
   - Compares the running image's local digest (`RepoDigests`) against the current registry digest via a single `HEAD` request.
   - Works on the **first scan** — no silent warm-up scan required.
   - For multi-arch images, falls back to a platform-specific digest check to avoid false positives when only another architecture's layer was updated.
   - Can be combined with `tag-regex` to also resolve the new digest to a versioned tag.
   - Takes precedence over semver detection when both labels are set.

3. POSTs all found updates to your webhook endpoint.

---

## Supported platforms

Pre-built images are published to `ghcr.io/stijnpiron/docker-update-monitor` for the following platforms:

| Platform       | Hardware                                                             |
| -------------- | -------------------------------------------------------------------- |
| `linux/amd64`  | x86-64 servers, cloud VMs, desktop Docker (Linux/Windows/Mac Intel)  |
| `linux/arm64`  | Raspberry Pi 4 / 5, Apple Silicon (via Docker Desktop), AWS Graviton |
| `linux/arm/v7` | Raspberry Pi 2 / 3 running 32-bit Raspberry Pi OS                    |

Docker automatically selects the correct image variant for your hardware — no `--platform` flag required.

---

## Quick start

```bash
# 1. Copy and fill in your credentials
cp .env.example .env
$EDITOR .env

# 2. Find your Docker socket GID
stat -c '%g' /var/run/docker.sock   # → e.g. 999

# 3. Start
docker compose up -d --build

# 4. Tail logs
docker compose logs -f
```

---

## Labelling your containers

Add labels to any container you want monitored:

```yaml
# docker-compose.yml (your app stack)
services:
  sonarr:
    image: linuxserver/sonarr:4.0.2.1183
    labels:
      # Required for semver detection — regex with capture groups for (major, minor, patch)
      docker-update-monitor.tag-regex: "^(\\d+)\\.(\\d+)\\.(\\d+)\\.(\\d+)$"

      # Optional — overrides auto-detected Compose project name
      # docker-update-monitor.stack: "media"

      # Optional — per-container cooldown, overrides the global UPDATE_COOLDOWN
      # docker-update-monitor.update-cooldown: "3d"

  nginx:
    image: nginx:1.25.3
    labels:
      docker-update-monitor.tag-regex: "^(\\d+)\\.(\\d+)\\.(\\d+)$"

  # Rolling-tag container monitored via explicit digest mode (no tag-regex needed)
  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:latest
    labels:
      docker-update-monitor.mode: "digest"

  # Rolling-tag with semver resolution: digest mode + tag-regex resolves the new
  # digest to a versioned tag when one is available
  myapp:
    image: myregistry/myapp:edge
    labels:
      docker-update-monitor.mode: "digest"
      docker-update-monitor.tag-regex: "^(\\d+)\\.(\\d+)\\.(\\d+)$"

  # Containers WITHOUT any monitoring label are silently ignored
  redis:
    image: redis:7
```

### Detection mode reference

| Labels set | Tag matches regex? | Mode used |
|---|---|---|
| `tag-regex` only | Yes | Semver |
| `tag-regex` only | No | Implicit digest (scan-to-scan comparison) |
| `mode: digest` only | — | Explicit digest (RepoDigests vs registry) |
| `mode: digest` + `tag-regex` | Any | Explicit digest (semver bypassed; regex used only for version resolution) |

> **Explicit vs implicit digest:** The explicit `mode: digest` label compares the running image's local digest against the registry on every scan, including the first. The implicit fallback (tag doesn't match `tag-regex`) compares the registry digest across two consecutive scans and is silent on the first. Use `mode: digest` when your container exclusively uses rolling tags and you want immediate detection.

### Regex tips

| Image tag format         | Regex                          |
| ------------------------ | ------------------------------ |
| `1.2.3`                  | `^(\d+)\.(\d+)\.(\d+)$`        |
| `v1.2.3`                 | `^v(\d+)\.(\d+)\.(\d+)$`       |
| `1.2.3.456` (four parts) | `^(\d+)\.(\d+)\.(\d+)\.(\d+)$` |
| `1.25.3-alpine`          | `^(\d+)\.(\d+)\.(\d+)-alpine$` |

The capture groups must be castable to integers and ordered
`(major, minor, patch [, build])`.

---

## Webhook payload

A JSON array is POSTed to `NOTIFY_ENDPOINT`. Each element represents one
update-level finding for one container:

```json
[
  {
    "container_name": "sonarr",
    "stack": "media",
    "image": "linuxserver/sonarr",
    "current_version": "4.0.2.1183",
    "new_version": "4.0.9.1835",
    "update_type": "patch"
  },
  {
    "container_name": "sonarr",
    "stack": "media",
    "image": "linuxserver/sonarr",
    "current_version": "4.0.2.1183",
    "new_version": "4.1.0.2000",
    "update_type": "minor"
  },
  {
    "container_name": "nginx",
    "stack": "proxy",
    "image": "library/nginx",
    "current_version": "1.25.3",
    "new_version": "1.27.4",
    "update_type": "minor"
  }
]
```

`update_type` is one of `patch`, `minor`, `major`, or `digest`.

For digest updates the `new_version` field contains either a resolved versioned tag (when one
could be matched to the new digest) or the raw registry digest (`sha256:…`).

---

## Environment variables

### General

| Variable             | Default          | Description                                                                                                                                                                                                |
| -------------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `NOTIFY_CHANNELS`    | `webhook`        | Comma-separated list of notification channels: `webhook`, `email`                                                                                                                                          |
| `DOCKERHUB_USERNAME` | _(empty)_        | Docker Hub username                                                                                                                                                                                        |
| `DOCKERHUB_PASSWORD` | _(empty)_        | Docker Hub password or PAT                                                                                                                                                                                 |
| `GITHUB_TOKEN`       | _(empty)_        | GitHub PAT with `read:packages` scope — required for ghcr.io images                                                                                                                                        |
| `CRON_SCHEDULE`      | `0 * * * *`      | Cron expression for check schedule (standard 5-field cron)                                                                                                                                                 |
| `RUN_ON_STARTUP`     | `true`           | Run an update check immediately on startup                                                                                                                                                                 |
| `DRY_RUN`            | `false`          | Log only, no notifications sent                                                                                                                                                                            |
| `LABEL_PREFIX`       | `update-monitor` | Label namespace                                                                                                                                                                                            |
| `UPDATE_COOLDOWN`    | `0`              | Global cooldown before notifying about a new update. Accepted formats: `0` (no cooldown), `12h`, `3d`, `2w`, `1m`. Can be overridden per container with the `docker-update-monitor.update-cooldown` label. |
| `LOG_LEVEL`          | `INFO`           | `DEBUG` / `INFO` / `WARNING` / `ERROR`                                                                                                                                                                     |
| `WEB_PORT`           | `8080`           | Port for the web dashboard and health endpoint                                                                                                                                                             |

### Webhook channel

| Variable            | Default   | Description                                      |
| ------------------- | --------- | ------------------------------------------------ |
| `NOTIFY_ENDPOINT`   | _(empty)_ | Webhook URL to POST updates to                   |
| `NOTIFY_AUTH_TYPE`  | _(empty)_ | Auth type: `bearer`, `basic`, or empty (no auth) |
| `NOTIFY_AUTH_TOKEN` | _(empty)_ | Token/credentials for the `Authorization` header |

### Email channel (SMTP)

| Variable        | Default   | Description                                        |
| --------------- | --------- | -------------------------------------------------- |
| `SMTP_HOST`     | _(empty)_ | SMTP server hostname (required for email)          |
| `SMTP_PORT`     | `587`     | SMTP server port                                   |
| `SMTP_USERNAME` | _(empty)_ | SMTP login username                                |
| `SMTP_PASSWORD` | _(empty)_ | SMTP login password                                |
| `SMTP_FROM`     | _(empty)_ | Sender email address (required for email)          |
| `SMTP_TO`       | _(empty)_ | Recipient(s), comma-separated (required for email) |
| `SMTP_TLS`      | `true`    | Use STARTTLS                                       |

> **Note:** If `NOTIFY_CHANNELS` includes `email` but `SMTP_HOST`, `SMTP_FROM`,
> or `SMTP_TO` are not set, the email channel logs a warning and is skipped —
> the application does not crash.

> **Note:** The `docker-compose.yml` in this repo sets `CRON_SCHEDULE` to
> `0 3 * * 7` (every Sunday at 03:00), overriding the code default of
> `0 * * * *` (every hour). Adjust the variable in your `.env` or
> `docker-compose.yml` to suit your needs.

---

## Update-level logic

Given current version `1.0.0` and available tags including
`1.0.1`, `1.0.6`, `1.1.0`, `1.2.0`, `2.0.0`:

| Level | Reported | Skipped             |
| ----- | -------- | ------------------- |
| patch | `1.0.6`  | `1.0.1`, `1.0.2`, … |
| minor | `1.2.0`  | `1.1.0`, `1.1.5`, … |
| major | `2.0.0`  | —                   |

Only the highest candidate per level is reported.

---

## Finding your Docker socket GID

```bash
stat -c '%g' /var/run/docker.sock
# Set DOCKER_GID in your .env to this value
```

---

## Web Dashboard

The monitor includes a built-in web dashboard accessible on port `8080` (configurable via `WEB_PORT`).

### Features

- **Summary cards** — containers monitored, new/known/resolved update counts, warnings, not-monitored count
- **Update table** — all detected updates with stack, container, image, versions, type, status, and first-seen date
- **Sortable columns** — click any column header to sort; default sort is by stack
- **Warnings section** — scan warnings and errors (invalid regex, missing tags, pattern mismatches)
- **Not Monitored section** — collapsible list of containers without any monitoring label (`tag-regex` or `mode: digest`), with reasons
- **Scan Now button** — trigger an immediate scan from the UI
- **Auto-refresh** — polls for changes every 60 seconds
- **Responsive** — works on desktop and mobile
- **No JavaScript required** — dashboard renders fully server-side (JS enhances with sorting, async scan + auto-refresh)

### Accessing the dashboard

Expose port `8080` (or your custom `WEB_PORT`) in your `docker-compose.yml`:

```yaml
services:
  docker-update-monitor:
    ports:
      - "8080:8080"
```

Then open `http://<your-host>:8080` in a browser.

### API endpoints

| Method | Path           | Description                                      |
| ------ | -------------- | ------------------------------------------------ |
| `GET`  | `/`            | Dashboard page (HTML)                            |
| `GET`  | `/health`      | Health check (JSON) — used by Docker HEALTHCHECK |
| `GET`  | `/api/updates` | All updates with status as JSON array            |
| `POST` | `/api/scan`    | Trigger immediate scan, returns 202 Accepted     |
| `GET`  | `/metrics`     | Prometheus metrics (text/plain)                  |

### Prometheus metrics

`GET /metrics` exposes the following metrics in Prometheus text format, updated after each scan:

| Metric | Type | Description |
| ------ | ---- | ----------- |
| `dum_containers_monitored` | Gauge | Number of containers with update-monitor labels |
| `dum_updates_available{type}` | Gauge | Active (non-resolved) updates by type (`patch`, `minor`, `major`, `digest`) |
| `dum_check_duration_seconds` | Gauge | Duration of the last update check in seconds |
| `dum_check_errors_total` | Counter | Total errors encountered during checks (Docker connection failures, registry warnings) |
| `dum_last_check_timestamp_seconds` | Gauge | Unix timestamp of the last completed check |
| `dum_notifications_sent_total{channel}` | Counter | Total notification dispatches by channel (`webhook`, `email`) |

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: docker-update-monitor
    static_configs:
      - targets: ["<your-host>:8080"]
```

### Environment variable

| Variable                    | Default          | Description                                         |
| --------------------------- | ---------------- | --------------------------------------------------- |
| `WEB_PORT`                  | `8080`           | Port for the web dashboard                          |
| `DASHBOARD_DATETIME_FORMAT` | `%d/%m/%Y %H:%M` | Python `strftime` format for dates on the dashboard |

---

## Integration ideas

- **n8n / Node-RED**: Point `NOTIFY_ENDPOINT` at an HTTP-trigger webhook node,
  then route the payload to Slack, Telegram, Gotify, ntfy, email, etc.
- **ntfy.sh**: `NOTIFY_ENDPOINT=https://ntfy.sh/your-topic` — ntfy accepts
  plain JSON as the message body out of the box.
- **Apprise / Gotify**: wrap in a small n8n flow or a tiny FastAPI receiver.

---

## Development

### Prerequisites

- Python 3.13+
- Docker (only for running the monitor itself, not needed for tests)

### Setup

```bash
# Clone the repo
git clone https://github.com/stijnpiron/docker-update-monitor.git
cd docker-update-monitor

# Create a virtual environment and install all dependencies (app + dev/test)
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes the runtime dependencies from `requirements.txt`
plus test tooling (`pytest`, `pytest-cov`). The Docker image only installs
`requirements.txt` to keep the production image lean.

### Running tests

```bash
# Run all tests with verbose output
.venv/bin/python -m pytest tests/ -v

# Run with coverage report
.venv/bin/python -m pytest tests/ --cov
```

All tests run without a Docker daemon — Docker calls are mocked.

### Running the monitor locally

```bash
# Set required env vars (or create a .env file)
export DRY_RUN=true
export RUN_ON_STARTUP=true

.venv/bin/python monitor.py
```

### Project structure

```
monitor.py              # Main application
requirements.txt        # Runtime dependencies (used in Docker image)
requirements-dev.txt    # Dev/test dependencies (includes requirements.txt)
pyproject.toml          # pytest & coverage configuration
Dockerfile              # Production container
docker-compose.yml      # Docker Compose deployment
tests/
├── conftest.py         # Shared fixtures
├── test_parse_tag.py   # parse_tag() unit tests
├── test_find_updates.py        # find_updates() logic
├── test_detect_registry.py     # detect_registry() table tests
├── test_image_parsing.py       # Image ref → name + tag splitting
├── test_notifications.py       # notify() behavior
├── test_webhook_auth.py        # Auth header tests
├── test_http_session.py        # HTTP session/retry config
├── test_regex_validation.py    # Invalid regex handling
├── test_graceful_shutdown.py   # SIGTERM/SIGINT handling
└── test_run_on_startup.py      # RUN_ON_STARTUP behavior
```
