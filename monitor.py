#!/usr/bin/env python3
"""
Docker Update Monitor
Monitors running containers for available image updates and notifies a webhook endpoint.

Label schema (add to your containers):
  update-monitor.tag-regex   Required. Regex with capture groups for version parts.
                             E.g.  ^v?(\\d+)\\.(\\d+)\\.(\\d+)$  for semantic versioning.
                             The groups must be comparable as integers (major, minor, patch).
  update-monitor.stack       Optional. Override stack name (auto-detected from Compose otherwise).

Environment variables:
  NOTIFY_ENDPOINT            Webhook URL to POST update payloads to.
  DOCKERHUB_USERNAME         Docker Hub username.
  DOCKERHUB_PASSWORD         Docker Hub password OR a Personal Access Token (PAT).
                             PATs are preferred — create one at hub.docker.com → Account Settings → Personal access tokens.
  CRON_SCHEDULE              Cron expression for check schedule (default: "0 * * * *" = every hour).
                             Supports standard 5-field cron: minute hour day month weekday.
                             Examples: "0 */6 * * *" (every 6h), "0 8 * * *" (daily at 08:00).
  LOG_LEVEL                  Logging level (default: INFO).
  LABEL_PREFIX               Label namespace (default: update-monitor).
  DRY_RUN                    Set to "true" to log updates without POSTing.
"""

import os
import re
import sys
import json
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional

import requests
import docker
from croniter import croniter
from datetime import datetime, timezone
from docker.errors import DockerException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NOTIFY_ENDPOINT   = os.environ.get("NOTIFY_ENDPOINT", "")
DOCKERHUB_USER    = os.environ.get("DOCKERHUB_USERNAME", "")
DOCKERHUB_PASS    = os.environ.get("DOCKERHUB_PASSWORD", "")
CRON_SCHEDULE     = os.environ.get("CRON_SCHEDULE", "0 * * * *")
LABEL_PREFIX      = os.environ.get("LABEL_PREFIX", "update-monitor")
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"
LOG_LEVEL         = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("dum")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class UpdateInfo:
    container_name: str
    stack: str
    image: str
    current_version: str
    new_version: str
    update_type: str        # "patch" | "minor" | "major"


# ---------------------------------------------------------------------------
# Docker Hub helpers
# ---------------------------------------------------------------------------

def get_dockerhub_token(username: str, password: str) -> Optional[str]:
    """
    Exchange DockerHub credentials for a JWT token.
    Both plain passwords and Personal Access Tokens (PATs) are accepted as `password`.
    PATs are recommended — generate one at:
    https://app.docker.com/settings/personal-access-tokens
    """
    if not username or not password:
        log.info("No DockerHub credentials provided; anonymous access (rate-limited).")
        return None
    try:
        resp = requests.post(
            "https://hub.docker.com/v2/users/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json().get("token")
        log.info("DockerHub authentication successful.")
        return token
    except Exception as exc:
        log.warning(f"DockerHub auth failed: {exc}. Falling back to anonymous access.")
        return None


def fetch_all_tags(image_name: str, token: Optional[str]) -> list[str]:
    """
    Fetch ALL tags for an image from Docker Hub in one paginated sweep,
    using the current /v2/namespaces/{namespace}/repositories/{repo}/tags API.
    image_name should be bare (no tag), e.g. "library/nginx" or "linuxserver/sonarr".
    Returns an empty list for non-DockerHub images.
    """
    # Resolve namespace/name
    parts = image_name.split("/")
    if len(parts) == 1:
        namespace, repo = "library", parts[0]
    elif len(parts) == 2:
        namespace, repo = parts
    else:
        log.debug(f"Skipping non-DockerHub image: {image_name}")
        return []

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    tags: list[str] = []
    # New Hub API path (replaces the legacy /v2/repositories/… route)
    url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repo}/tags?page_size=100"

    while url:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            tags.extend(t["name"] for t in data.get("results", []))
            url = data.get("next")  # None when last page reached
        except requests.HTTPError as exc:
            log.error(f"HTTP error fetching tags for {namespace}/{repo}: {exc}")
            break
        except Exception as exc:
            log.error(f"Error fetching tags for {namespace}/{repo}: {exc}")
            break

    log.info(f"  Fetched {len(tags):4d} tags  ←  {namespace}/{repo}")
    return tags


# ---------------------------------------------------------------------------
# Version parsing & comparison
# ---------------------------------------------------------------------------

def parse_tag(tag: str, pattern: str) -> Optional[tuple[int, ...]]:
    """
    Match `tag` against `pattern` (fullmatch) and return all capture groups as ints.
    Returns None if the tag doesn't match or groups can't be cast to int.
    """
    m = re.fullmatch(pattern, tag)
    if not m:
        return None
    try:
        return tuple(int(g) for g in m.groups())
    except (ValueError, TypeError):
        return None


def find_updates(
    current_tag: str,
    all_tags: list[str],
    pattern: str,
) -> dict[str, str]:
    """
    Given the current tag and a list of all available tags (filtered by `pattern`),
    return up to three "best" tags — one per update level.

    Rules (assuming semver-style groups: major, minor, patch):
      patch  — same major + minor, higher patch  → report only the highest patch
      minor  — same major, higher minor           → report only the highest minor
      major  — higher major                       → report only the highest major

    Only the *best* (highest) candidate per level is returned, so you'll
    never get spammed with every intermediate version.
    """
    current = parse_tag(current_tag, pattern)
    if current is None or len(current) < 3:
        log.warning(f"    Cannot parse current tag '{current_tag}' with pattern '{pattern}'")
        return {}

    cur_maj, cur_min, cur_pat = current[0], current[1], current[2]

    # (version_tuple, tag_string) per level
    best: dict[str, tuple[tuple, str]] = {}

    for tag in all_tags:
        v = parse_tag(tag, pattern)
        if v is None or len(v) < 3:
            continue
        maj, minor, pat = v[0], v[1], v[2]

        if maj == cur_maj and minor == cur_min and pat > cur_pat:
            level = "patch"
        elif maj == cur_maj and minor > cur_min:
            level = "minor"
        elif maj > cur_maj:
            level = "major"
        else:
            continue  # older or equal — skip

        current_best = best.get(level)
        if current_best is None or v > current_best[0]:
            best[level] = (v, tag)

    return {level: tag for level, (_, tag) in best.items()}


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def notify(updates: list[UpdateInfo]) -> None:
    if not updates:
        return

    payload = [asdict(u) for u in updates]

    if DRY_RUN:
        log.info("DRY_RUN — would POST:\n" + json.dumps(payload, indent=2))
        return

    if not NOTIFY_ENDPOINT:
        log.warning("No NOTIFY_ENDPOINT set; skipping notification.")
        log.info("Updates found:\n" + json.dumps(payload, indent=2))
        return

    try:
        resp = requests.post(
            NOTIFY_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        log.info(f"Notified endpoint with {len(updates)} update(s)  →  HTTP {resp.status_code}")
    except Exception as exc:
        log.error(f"Failed to notify endpoint: {exc}")


# ---------------------------------------------------------------------------
# Core check loop
# ---------------------------------------------------------------------------

def run_check() -> None:
    log.info("=" * 60)
    log.info("Starting update check")
    log.info("=" * 60)

    try:
        client = docker.from_env()
    except DockerException as exc:
        log.error(f"Cannot connect to Docker: {exc}")
        return

    token = get_dockerhub_token(DOCKERHUB_USER, DOCKERHUB_PASS)

    containers = client.containers.list()
    log.info(f"Running containers: {len(containers)}")

    # Cache tag lists — one fetch per unique image, shared across containers
    tags_cache: dict[str, list[str]] = {}
    all_updates: list[UpdateInfo] = []

    for container in containers:
        labels  = container.labels
        pattern = labels.get(f"{LABEL_PREFIX}.tag-regex")

        if not pattern:
            log.debug(f"  [{container.name}] No '{LABEL_PREFIX}.tag-regex' label — skipping")
            continue

        # Resolve full image reference
        image_ref = None
        if container.image.tags:
            image_ref = container.image.tags[0]
        else:
            # Fallback: read from container attrs
            image_ref = container.attrs.get("Config", {}).get("Image", "")

        if not image_ref:
            log.warning(f"  [{container.name}] Cannot determine image reference — skipping")
            continue

        # Split into name + tag
        # Handle registry prefixes: registry.example.com:5000/ns/image:tag
        # Strategy: split on the last colon that follows a slash (or the only colon if no slashes after it)
        if ":" in image_ref.split("/")[-1]:
            image_name, current_tag = image_ref.rsplit(":", 1)
        else:
            image_name, current_tag = image_ref, "latest"

        # Detect stack (Compose sets this automatically)
        stack = (
            labels.get(f"{LABEL_PREFIX}.stack")
            or labels.get("com.docker.compose.project")
            or "standalone"
        )

        log.info(f"  [{container.name}]  image={image_name}:{current_tag}  stack={stack}")

        # Fetch tags once per unique image name
        if image_name not in tags_cache:
            tags_cache[image_name] = fetch_all_tags(image_name, token)

        all_tags = tags_cache[image_name]
        if not all_tags:
            log.warning(f"    No tags returned for {image_name} — skipping")
            continue

        updates = find_updates(current_tag, all_tags, pattern)

        if not updates:
            log.info(f"    No updates found (current={current_tag})")
        else:
            for update_type, new_tag in updates.items():
                log.info(f"    {update_type.upper():5s} update: {current_tag} → {new_tag}")
                all_updates.append(UpdateInfo(
                    container_name=container.name,
                    stack=stack,
                    image=image_name,
                    current_version=current_tag,
                    new_version=new_tag,
                    update_type=update_type,
                ))

    log.info("-" * 60)
    log.info(f"Check complete — {len(all_updates)} update(s) to report")
    notify(all_updates)


def main() -> None:
    log.info("Docker Update Monitor started")
    if DRY_RUN:
        log.info("DRY_RUN mode active — no HTTP POSTs will be made")

    if not croniter.is_valid(CRON_SCHEDULE):
        log.error(f"Invalid cron expression: '{CRON_SCHEDULE}' — exiting")
        sys.exit(1)

    log.info(f"Schedule: '{CRON_SCHEDULE}'")

    cron = croniter(CRON_SCHEDULE, datetime.now(timezone.utc))
    next_run = cron.get_next(datetime)
    log.info(f"Next check at: {next_run.strftime('%Y-%m-%dT%H:%M:%S %Z')}")

    while True:
        now = datetime.now(timezone.utc)
        wait = (next_run - now).total_seconds()

        if wait > 0:
            time.sleep(wait)

        run_check()

        next_run = cron.get_next(datetime)
        log.info(f"Next check at: {next_run.strftime('%Y-%m-%dT%H:%M:%S %Z')}\n")


if __name__ == "__main__":
    main()
