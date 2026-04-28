# Docker Update Monitor

Monitors all running Docker containers for image updates and POSTs a structured
payload to a webhook when updates are found.

## How it works

1. Reads all running containers from the Docker socket.
2. For each container that has the `docker-update-monitor.tag-regex` label, it:
   - Determines the current image tag.
   - Fetches **all** available tags from Docker Hub in a single paginated sweep
     (tags are cached per image, so multiple containers sharing the same image
     only trigger one API call).
   - Applies your regex to parse version numbers from each tag.
   - Finds the **best** (highest) update per level — one for patch, one for
     minor, one for major — and deduplicates. You never receive intermediate
     versions, only the latest available at each update level.
3. POSTs all found updates to your webhook endpoint.

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
      # Required — regex with capture groups for (major, minor, patch)
      docker-update-monitor.tag-regex: "^(\\d+)\\.(\\d+)\\.(\\d+)\\.(\\d+)$"

      # Optional — overrides auto-detected Compose project name
      # docker-update-monitor.stack: "media"

  nginx:
    image: nginx:1.25.3
    labels:
      docker-update-monitor.tag-regex: "^(\\d+)\\.(\\d+)\\.(\\d+)$"

  # Containers WITHOUT the label are silently ignored
  redis:
    image: redis:7
```

### Regex tips

| Image tag format | Regex |
|---|---|
| `1.2.3` | `^(\d+)\.(\d+)\.(\d+)$` |
| `v1.2.3` | `^v(\d+)\.(\d+)\.(\d+)$` |
| `1.2.3.456` (four parts) | `^(\d+)\.(\d+)\.(\d+)\.(\d+)$` |
| `1.25.3-alpine` | `^(\d+)\.(\d+)\.(\d+)-alpine$` |

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

`update_type` is one of `patch`, `minor`, or `major`.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_ENDPOINT` | *(empty)* | Webhook URL to POST updates to |
| `DOCKERHUB_USERNAME` | *(empty)* | Docker Hub username |
| `DOCKERHUB_PASSWORD` | *(empty)* | Docker Hub password or PAT |
| `POLL_INTERVAL` | `3600` | Seconds between checks |
| `DRY_RUN` | `false` | Log only, no HTTP POSTs |
| `LABEL_PREFIX` | `update-monitor` | Label namespace |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Update-level logic

Given current version `1.0.0` and available tags including
`1.0.1`, `1.0.6`, `1.1.0`, `1.2.0`, `2.0.0`:

| Level | Reported | Skipped |
|---|---|---|
| patch | `1.0.6` | `1.0.1`, `1.0.2`, … |
| minor | `1.2.0` | `1.1.0`, `1.1.5`, … |
| major | `2.0.0` | — |

Only the highest candidate per level is reported.

---

## Finding your Docker socket GID

```bash
stat -c '%g' /var/run/docker.sock
# Set DOCKER_GID in your .env to this value
```

---

## Integration ideas

- **n8n / Node-RED**: Point `NOTIFY_ENDPOINT` at an HTTP-trigger webhook node,
  then route the payload to Slack, Telegram, Gotify, ntfy, email, etc.
- **ntfy.sh**: `NOTIFY_ENDPOINT=https://ntfy.sh/your-topic` — ntfy accepts
  plain JSON as the message body out of the box.
- **Apprise / Gotify**: wrap in a small n8n flow or a tiny FastAPI receiver.
