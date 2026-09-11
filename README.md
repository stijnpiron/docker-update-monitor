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

| Platform      | Hardware                                                             |
| ------------- | -------------------------------------------------------------------- |
| `linux/amd64` | x86-64 servers, cloud VMs, desktop Docker (Linux/Windows/Mac Intel)  |
| `linux/arm64` | Raspberry Pi 4 / 5, Apple Silicon (via Docker Desktop), AWS Graviton |

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

| Labels set                   | Tag matches regex? | Mode used                                                                 |
| ---------------------------- | ------------------ | ------------------------------------------------------------------------- |
| `tag-regex` only             | Yes                | Semver                                                                    |
| `tag-regex` only             | No                 | Implicit digest (scan-to-scan comparison)                                 |
| `mode: digest` only          | —                  | Explicit digest (RepoDigests vs registry)                                 |
| `mode: digest` + `tag-regex` | Any                | Explicit digest (semver bypassed; regex used only for version resolution) |

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
    "host": "local",
    "image": "linuxserver/sonarr",
    "current_version": "4.0.2.1183",
    "new_version": "4.0.9.1835",
    "update_type": "patch"
  },
  {
    "container_name": "nginx",
    "stack": "proxy",
    "host": "prod",
    "image": "library/nginx",
    "current_version": "1.25.3",
    "new_version": "1.27.4",
    "update_type": "minor"
  }
]
```

`update_type` is one of `patch`, `minor`, `major`, or `digest`. Each row carries
a `host` field — the daemon the finding came from (`local`, or a name from
`DOCKER_HOSTS`).

In addition to the update payload, a host that becomes unreachable raises a
separate **host-status** alert, and a host that comes back up raises a
`recovered` alert:

```json
{
  "type": "host_status",
  "host": "prod",
  "event": "down",
  "error": "ssh: Connection timed out"
}
```

`event` is `down` or `recovered`. These alerts are coalesced per host by
`HOST_REACH_COOLDOWN`. For consumers of the payload: `error` is only
meaningful when `event` is `down` (it carries the connection failure
message); for `recovered` events it is always `null`.

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

### Multi-host scanning over SSH (optional)

By default the monitor scans only the local Docker socket. Set `DOCKER_HOSTS` to
also scan one or more remote daemons over SSH from this single instance, all on
the same `CRON_SCHEDULE`:

| Variable              | Default   | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DOCKER_HOSTS`        | _(empty)_ | Comma-separated `name=ssh://user@host` pairs scanned in addition to the local daemon. Each `name` is the stable per-host label (shown in the dashboard and notifications), restricted to `A-Za-z0-9._-`, and must be unique and not collide with the local daemon's name (see `LOCAL_HOST_NAME`). Only the `ssh://` scheme is supported. When unset, behavior is identical to before this feature (local only).                                                                                                                                                                                                                                                            |
| `HOST_REACH_COOLDOWN` | `1h`      | Re-alert window for a host down/recovered alert. Accepted formats: `15m`, `1h`, `2d`, `1w`. A repeat transition for the same host inside this window is coalesced (no new alert); after it elapses the alert refires.                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `LOCAL_HOST_NAME`     | `local`   | Label used for the local daemon in the dashboard, webhook payloads, and the `host` Prometheus label (where the default `local` used to appear). Same character set and uniqueness rules as `DOCKER_HOSTS` names — a remote host cannot reuse it (fails fast at startup). When you first set a new value, a one-time data migration renames the `local` rows already stored in `data/state.db` so existing update history, host-status rows, and down/recovered alert continuity follow the new name. Set it in your secret manager (Infisical or similar) alongside the other `DOCKER_HOSTS`-related values; it is a plain env var like every other setting in this table. |

**Working example** — scan the local daemon plus two remote hosts:

```
DOCKER_HOSTS=prod=ssh://monitor@prod-host,nas=ssh://monitor@nas-host
HOST_REACH_COOLDOWN=1h
```

**Renaming the local daemon** — label the local daemon `home-node` instead of
`local` (existing history migrates to the new label on first run):

```
LOCAL_HOST_NAME=home-node
```

The value lives in the same `.env` (or Infisical) as `DOCKER_HOSTS`; no code
or image change is required. `local` remains a perfectly good name for a
_remote_ host once the local daemon has been renamed.

**Validation (fail fast at startup).** A malformed `DOCKER_HOSTS` value — a pair
missing `=`, a duplicate host name, a name equal to the local daemon's name
(the `LOCAL_HOST_NAME` value, default `local`), a name with invalid characters,
an empty name, or a `docker_host_url` scheme other than `ssh://` — is logged
and exits with status `1` before the scheduler starts, the same way an invalid
`CRON_SCHEDULE` does. It is never silently dropped and discovered later as
"host unreachable". A `LOCAL_HOST_NAME` with invalid characters fails the same
way.

**SSH setup — step by step.** The monitor image bundles `openssh-client`. The
scanner builds each remote `DockerClient` with `use_ssh_client=True`, so the
system `ssh` binary resolves identity per host from a standard `~/.ssh/config`
— no key material ever appears in `DOCKER_HOSTS`. There are at most two machines
you touch per remote host:

| Machine                                                         | Role        | What you do here                                                                                                                                                     |
| --------------------------------------------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Monitor host** — the box running the `docker compose` service | Client side | Set the env vars, create the SSH config + per-host key files, mount them, (re)start the service                                                                      |
| **Remote Docker host** — e.g. `prod-host`                       | Server side | Ensure the user you SSH in as can run `docker` commands (i.e. is root or in the `docker` group), and that user's `authorized_keys` contains the monitor's public key |

Work through it in order. Replace `prod-host`, `monitor`, and `ssh_prod_key`
with your own values. Step 1 creates the key; step 2 installs its public half
on each remote — that's the step the `authorized_keys` line in the table above
refers to.

**1. Monitor host — generate one private key per remote host.** One key per
remote host so a leaked key only exposes one daemon. Create them on the monitor
host (their paths are the `./config/ssh/…` files the compose service mounts in
step 5). Do this first, because step 2 installs each remote's _public_ half.

```bash
# on the monitor host
mkdir -p ./config/ssh/.ssh
ssh-keygen -t ed25519 -N '' -f ./config/ssh/ssh_prod_key     # private  (keep 600)
# repeat one per remote host:
# ssh-keygen -t ed25519 -N '' -f ./config/ssh/ssh_nas_key
```

**2. Remote Docker host — install the public key (and allow docker).** Install
each remote's public half into that host's `authorized_keys`, and confirm the
account can reach the daemon. Run this **from the monitor host** — it `ssh`es
out to the remote, so it changes the remote box.

```bash
# on the monitor host — install prod-host's public half
ssh-copy-id -i ./config/ssh/ssh_prod_key.pub monitor@prod-host
# one per remote host:
# ssh-copy-id -i ./config/ssh/ssh_nas_key.pub  monitor@nas-host
```

If you don't have `ssh-copy-id`, the equivalent is:

```bash
ssh monitor@prod-host 'umask 077; mkdir -p ~/.ssh'
cat ./config/ssh/ssh_prod_key.pub | ssh monitor@prod-host \
    'cat >> ~/.ssh/authorized_keys'
```

The first connection to each host will normally ask for that account's password
once; key auth (what the monitor uses afterwards) then needs no password.
If it instead fails with `Permission denied (publickey)` and _never_ asks for a
password, the remote's sshd has password auth disabled — `ssh-copy-id` needs one
interactive login to bootstrap, so install the public half through a channel
that already gets onto the box (a root/other key, the console, or a temporarily
re-enabled password), e.g. from the monitor host:

```bash
cat ./config/ssh/ssh_prod_key.pub | ssh root@prod-host \
    'install -d -m 700 -o monitor -g monitor /home/monitor/.ssh
     touch /home/monitor/.ssh/authorized_keys
     chown monitor:monitor /home/monitor/.ssh/authorized_keys
     chmod 644 /home/monitor/.ssh/authorized_keys
     cat >> /home/monitor/.ssh/authorized_keys'
```

Note: a group- or world-writable `authorized_keys` makes the remote sshd reject
the key — keep `~/.ssh` at `700` and `authorized_keys` at `600`/`644`.

Give the account docker access (run on the **remote** host, as root):

```bash
usermod -aG docker monitor
```

Then prove the whole client path works before you wire up compose — this is the
same call the monitor makes, and it must print a version:

```bash
ssh -i ./config/ssh/ssh_prod_key -o BatchMode=yes monitor@prod-host \
    'docker version --format "{{.Server.Version}}"'
```

If that prints a version, the remote side is fully ready. The private key only
ever exists on the monitor host; each remote gets only the public half that
corresponds to it. Blast-radius isolation comes from one private key per `Host`
block, not from separate remote accounts — a single dedicated `monitor` account
per fleet is fine.

**3. Monitor host — write the shared SSH config + a writable
`known_hosts`.** Two files, both on the monitor host. The config maps each
`Host <alias>` to its per-host key and to the pinned `known_hosts`.
`UserKnownHostsFile` + `StrictHostKeyChecking accept-new` is the
**default, recommended** setup — it records each remote's host key on first
contact (one warning) and afterwards _refuses_ a connection whose host key has
changed (the change is recorded, the host is marked `unreachable`). This is
stronger than the old read-only default, which only warned and never verified.

```bash
# on the monitor host
cat > ./config/ssh/.ssh/config <<'EOF'
Host prod-host
    IdentityFile /run/secrets/ssh_prod_key
    StrictHostKeyChecking accept-new
    UserKnownHostsFile /home/ssh/known_hosts

Host nas-host
    IdentityFile /run/secrets/ssh_nas_key
    StrictHostKeyChecking accept-new
    UserKnownHostsFile /home/ssh/known_hosts
EOF
touch ./config/ssh/known_hosts          # starts EMPTY on purpose
sudo chown root:root ./config/ssh/.ssh/config
chmod 644 ./config/ssh/.ssh/config
```

- **The config file's owner matters, not just its mode.** OpenSSH refuses to
  read a user config file unless it is owned by `root` **or** by the account
  running `ssh` (`nobody`, uid 65534 in this image) — any other owner fails
  with `Bad owner or permissions on /home/ssh/.ssh/config`, even at `644`, and
  every host is recorded unreachable. `chown root:root` (above) is the
  simplest fix since `root` always satisfies the check regardless of the
  container's uid. Also keep the mode free of group/other **write** bits
  (`644`/`600` are fine, `664`/`646` are not).
- The second file, `./config/ssh/known_hosts`, gets **nothing from you — it
  starts empty on purpose**. On the first connection, `accept-new` records each
  remote's host key into it automatically (you'll see one warning per host in
  the logs); afterwards every connection is checked against the recorded key
  and a mismatch is refused. Leave the file writable (that's what lets ssh
  write into it). You can inspect it any time — one line per host, e.g.
  `prod-host ssh-ed25519 AAAA...`. **Never** hand-edit it, and if a host key
  _legitimately_ changes (host rebuilt), delete just that one line — or the
  host will stay `unreachable`.
- **The `Host <pattern>` line must match the literal connect string** — the
  part after `@` in the `ssh://user@host` URL you put in `DOCKER_HOSTS` — NOT
  the dashboard `name=` label before the `=`. These only look the same when
  that part is a DNS alias (e.g. `ssh://monitor@prod-host` → `Host
prod-host`). If you instead connect by IP (`ssh://docker-adm@192.168.1.44`),
  the block must be `Host 192.168.1.44`; a `Host` line matching the dashboard
  label alone never matches, so ssh falls back to its non-`accept-new`
  default and fails hard with `Host key verification failed` in the
  non-interactive session docker-py spawns. Want to keep a friendly alias as
  the `Host` name anyway? Add `HostName 192.168.1.44` inside the block.
- The paths inside the config (`/run/secrets/…`, `/home/ssh/known_hosts`) are
  **in-container** paths; the _source_ files on the monitor host are the
  relative `./config/ssh/…` paths, wired to those container paths in step 5.

**4. Monitor host — set the environment variables.** In the `.env` file next to
`docker-compose.yml` (or your Infisical/secret store that injects the service's
env):

```bash
DOCKER_HOSTS=prod=ssh://monitor@prod-host,nas=ssh://monitor@nas-host
HOST_REACH_COOLDOWN=1h
# Optional: rename the local daemon's label (default "local")
# LOCAL_HOST_NAME=home-node
```

**5. Monitor host — wire the volumes, secrets, and service env in
`docker-compose.yml`.** Uncomment/enable the three commented blocks in
`docker-compose.yml`. Every `./config/ssh/…` path below resolves **relative to
the compose file itself** — the keys, the SSH config, and `known_hosts` must
live in a `config/ssh/` directory _next to the `docker-compose.yml` you
deploy_, not in your `~` (files built elsewhere fail at deploy with
`bind source path does not exist: …/config/ssh/…`).

- the service `environment` line for `DOCKER_HOSTS` (and, optionally,
  `LOCAL_HOST_NAME`),
- the service `volumes` entry that mounts the config read-only **and** the
  `known_hosts` file writable,
- the service `secrets:` list plus the top-level `secrets:` block that maps each
  key file into `/run/secrets/…`.

The exact entries are commented in `docker-compose.yml`; together they become:

```yaml
volumes:
  - ./config/ssh/.ssh:/home/ssh/.ssh:ro # config is read-only
  - ./config/ssh/known_hosts:/home/ssh/known_hosts # writable (no :ro) → pinning works
secrets:
  - ssh_prod_key
  - ssh_nas_key
```

and at top level:

```yaml
secrets:
  ssh_prod_key:
    file: ./config/ssh/ssh_prod_key
  ssh_nas_key:
    file: ./config/ssh/ssh_nas_key
```

**The key must be world-readable on the host.** Under plain `docker compose up`
(not `docker stack deploy`/Swarm), a `file:` secret is a **bind mount of the
host file** — Compose's own docs note `uid`/`gid`/`mode` are only honored for
`environment:`-sourced secrets and are silently ignored for `file:` ones. So
the container sees the host key's exact owner and mode; it is **not**
published as `root:root 0444` (that behavior is Swarm-only). Since the
container runs `ssh` as `nobody` (uid 65534):

```bash
chmod 644 ./config/ssh/ssh_prod_key ./config/ssh/ssh_nas_key
```

The owner can stay whoever created the key (e.g. your deploy user) — OpenSSH's
private-key safety check only rejects a key _owned by_ the running user
(`nobody`) with group/other bits set, so a non-`nobody` owner plus `644` is
safe. Skipping this leaves the key unreadable by `nobody` and every connect
fails with `Load key "/run/secrets/…": Permission denied` /
`Permission denied (publickey)`.

**6. Monitor host — (re)start and verify.**

```bash
docker compose up -d
docker logs docker-update-monitor | grep -i 'host\|ssh'
# or hit the dashboard /metrics
```

Each host appears in the dashboard status strip and the
`dum_host_reachable{host}` gauge. The first scan records each remote's host
key into `known_hosts` (a one-time warning in the logs); later scans are silent,
and a host that changes its key thereafter fails and is shown `unreachable`.
If a host is `unreachable`, the dashboard row carries the exact ssh error
(typically a missing config/key mount, a `Host <alias>` name mismatch, or a
key that wasn't authorized on the remote).

**Unreachable hosts** are skipped (never fatal), recorded per host in
`host_status`, and surfaced in the dashboard status strip. A **host down**
transition raises one "unreachable" alert; a **recovered** transition raises one
"recovered" alert; both are coalesced by `HOST_REACH_COOLDOWN`.

### Webhook channel

| Variable            | Default   | Description                                                                                                                       |
| ------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `NOTIFY_ENDPOINT`   | _(empty)_ | Webhook URL to POST updates to                                                                                                    |
| `NOTIFY_AUTH_TYPE`  | _(empty)_ | Auth type: `bearer`, `basic`, or empty (no auth)                                                                                  |
| `NOTIFY_AUTH_TOKEN` | _(empty)_ | Credentials for the `Authorization` header. For `bearer`, the raw token. For `basic`, `user:pass` (base64-encoded automatically). |

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
- **Host status strip** — one chip per configured host (green up / red down / grey unknown) with the last error shown for any down host
- **Update table** — all detected updates with a **Host** column, plus stack, container, image, versions, type, status, and first-seen date
- **Sortable columns** — click any column header to sort; default sort is by host, then stack
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

| Method | Path               | Description                                                                                                                                                         |
| ------ | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET`  | `/`                | Dashboard page (HTML)                                                                                                                                               |
| `GET`  | `/health`          | Liveness check (JSON) — used by Docker HEALTHCHECK. Always `200` while the server is up; body `status` is `"starting"` until the first scan completes, then `"ok"`. |
| `GET`  | `/api/updates`     | All updates with status as JSON array (each row includes its `host`)                                                                                                |
| `GET`  | `/api/host-status` | Per-host reachability snapshot as a JSON array (`host`, `reachable`, `error`, `checked_at`)                                                                         |
| `POST` | `/api/scan`        | Trigger immediate scan, returns 202 Accepted                                                                                                                        |
| `GET`  | `/metrics`         | Prometheus metrics (text/plain)                                                                                                                                     |

### Prometheus metrics

`GET /metrics` exposes the following metrics in Prometheus text format, updated after each scan:

| Metric                                  | Type    | Description                                                                                                                                                                    |
| --------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `dum_containers_monitored`              | Gauge   | Number of containers with update-monitor labels                                                                                                                                |
| `dum_updates_available{type,host}`      | Gauge   | Active (non-resolved) updates by type (`patch`, `minor`, `major`, `digest`) and host                                                                                           |
| `dum_host_reachable{host}`              | Gauge   | Per-host reachability gauge (`1` = reachable, `0` = unreachable). Only present while `DOCKER_HOSTS` scans remote hosts; the local-only default still emits the `local` series. |
| `dum_check_duration_seconds`            | Gauge   | Duration of the last update check in seconds                                                                                                                                   |
| `dum_check_errors_total`                | Counter | Total errors encountered during checks (Docker connection failures, registry warnings)                                                                                         |
| `dum_last_check_timestamp_seconds`      | Gauge   | Unix timestamp of the last completed check                                                                                                                                     |
| `dum_notifications_sent_total{channel}` | Counter | Total notification dispatches by channel (`webhook`, `email`)                                                                                                                  |

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

### Dependency locking

Dependencies are managed with [`pip-tools`](https://github.com/jazzband/pip-tools).
The human-edited inputs are `requirements.in` and `requirements-dev.in`; the
fully-pinned, hashed `requirements.txt` and `requirements-dev.txt` are generated
from them and are what gets installed everywhere (Docker image, CI, local dev).
Pinning every transitive dependency with a hash gives reproducible builds and
guards against supply-chain surprises.

To add, remove, or bump a dependency:

```bash
# 1. Edit the .in file (e.g. add a new line to requirements.in)
$EDITOR requirements.in

# 2. Re-lock — installs pip-tools if needed
.venv/bin/pip install pip-tools
.venv/bin/pip-compile --generate-hashes --output-file=requirements.txt requirements.in
.venv/bin/pip-compile --generate-hashes --output-file=requirements-dev.txt requirements-dev.in

# 3. Sync your venv to the new lock
.venv/bin/pip install -r requirements-dev.txt
```

Dependabot opens PRs against the `.in` files; after merging a bump, re-run
`pip-compile` to refresh the lock files.

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
requirements.in         # Runtime dependency declarations (human-edited)
requirements.txt        # Locked runtime deps with hashes (used in Docker image)
requirements-dev.in     # Dev/test dependency declarations (human-edited)
requirements-dev.txt    # Locked dev/test deps with hashes
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
