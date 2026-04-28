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
  GITHUB_TOKEN               GitHub PAT with read:packages scope — required for ghcr.io images.
                             Generate one at: https://github.com/settings/tokens
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
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
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

# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def detect_registry(image_name: str) -> str:
    """Return 'ghcr' | 'dockerhub' | 'unknown' based on the image name prefix."""
    if image_name.startswith("ghcr.io/"):
        return "ghcr"
    # docker.io prefix is sometimes explicit but usually omitted
    if "/" not in image_name or image_name.startswith("docker.io/"):
        return "dockerhub"
    # Two-part names like "linuxserver/sonarr" are DockerHub namespaced images
    parts = image_name.split("/")
    if len(parts) == 2 and "." not in parts[0]:
        return "dockerhub"
    return "unknown"


def get_dockerhub_token(username: str, password: str) -> Optional[str]:
    """
    Exchange DockerHub credentials for a JWT token.
    Both plain passwords and Personal Access Tokens (PATs) are accepted as `password`.
    PATs are recommended — generate one at:
    https://app.docker.com/settings/personal-access-tokens
    """
    if not username or not password:
        log.info("DockerHub: no credentials — using anonymous access (rate-limited).")
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


def _fetch_dockerhub_tags(image_name: str, token: Optional[str], current_tag: Optional[str] = None) -> list[str]:
    """Fetch tags from Docker Hub using the Hub v2 API.

    Docker Hub returns tags ordered by last_updated descending (newest first).
    When *current_tag* is given we stop paginating once the current tag appears
    on a page, because all subsequent pages contain only older entries.
    """
    # Strip explicit docker.io/ prefix if present
    name = image_name.removeprefix("docker.io/")
    parts = name.split("/")
    namespace = "library" if len(parts) == 1 else parts[0]
    repo = parts[-1]

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    tags: list[str] = []
    url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repo}/tags?page_size=100&ordering=last_updated"

    while url:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            page_tags = [t["name"] for t in data.get("results", [])]
            tags.extend(page_tags)

            # Early stop: current tag found on this page → older pages not needed
            if current_tag and current_tag in page_tags:
                log.debug(f"DockerHub: found current tag '{current_tag}' — stopping pagination")
                break

            url = data.get("next")
        except requests.HTTPError as exc:
            log.error(f"DockerHub HTTP error for {namespace}/{repo}: {exc}")
            break
        except Exception as exc:
            log.error(f"DockerHub error for {namespace}/{repo}: {exc}")
            break

    return tags


def _get_ghcr_token(owner: str, repo: str, github_token: str) -> Optional[str]:
    """Exchange a GitHub PAT for a short-lived GHCR pull token."""
    import base64
    # GHCR uses the standard OCI token endpoint
    auth = base64.b64encode(f"token:{github_token}".encode()).decode()
    try:
        resp = requests.get(
            f"https://ghcr.io/token?service=ghcr.io&scope=repository:{owner}/{repo}:pull",
            headers={"Authorization": f"Basic {auth}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("token")
    except Exception as exc:
        log.warning(f"GHCR token exchange failed for {owner}/{repo}: {exc}")
        return None


def _fetch_ghcr_tags(image_name: str, github_token: str) -> list[str]:
    """
    Fetch all tags from GitHub Container Registry using the OCI v2 API.
    image_name should include the ghcr.io/ prefix, e.g. ghcr.io/owner/repo.
    Pagination follows the Link: <url>; rel="next" header.
    """
    # Strip ghcr.io/ → owner/repo (may be owner/repo or owner/group/repo)
    path = image_name.removeprefix("ghcr.io/")
    parts = path.split("/")
    if len(parts) < 2:
        log.warning(f"GHCR: cannot parse owner/repo from '{image_name}'")
        return []
    owner = parts[0]
    repo = "/".join(parts[1:])

    if not github_token:
        log.warning(f"GHCR: no GITHUB_TOKEN set — cannot fetch tags for {image_name}. "
                    "Set GITHUB_TOKEN with read:packages scope.")
        return []

    pull_token = _get_ghcr_token(owner, repo, github_token)
    if not pull_token:
        return []

    headers = {"Authorization": f"Bearer {pull_token}"}
    tags: list[str] = []
    url = f"https://ghcr.io/v2/{owner}/{repo}/tags/list?n=100"

    while url:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            tags.extend(data.get("tags") or [])

            # OCI pagination uses the Link header, not a next field in the body
            link = resp.headers.get("Link", "")
            next_url = None
            for part in link.split(","):
                part = part.strip()
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break
            url = next_url
        except requests.HTTPError as exc:
            log.error(f"GHCR HTTP error for {owner}/{repo}: {exc}")
            break
        except Exception as exc:
            log.error(f"GHCR error for {owner}/{repo}: {exc}")
            break

    return tags


def fetch_all_tags(image_name: str, dockerhub_token: Optional[str], github_token: str,
                   current_tag: Optional[str] = None) -> list[str]:
    """Route to the correct registry fetcher based on the image name."""
    registry = detect_registry(image_name)

    if registry == "dockerhub":
        tags = _fetch_dockerhub_tags(image_name, dockerhub_token, current_tag)
        log.info(f"  Fetched {len(tags):4d} tags  ←  DockerHub  {image_name}")
    elif registry == "ghcr":
        tags = _fetch_ghcr_tags(image_name, github_token)
        log.info(f"  Fetched {len(tags):4d} tags  ←  GHCR       {image_name}")
    else:
        log.warning(f"  Unsupported registry for '{image_name}' — skipping. "
                    "Only DockerHub and ghcr.io are supported.")
        tags = []

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
    if not m.groups():
        raise ValueError(f"Pattern '{pattern}' matched '{tag}' but has no capture groups — wrap each version number in ()")
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
    try:
        current = parse_tag(current_tag, pattern)
    except ValueError as exc:
        log.warning(f"    {exc}")
        return {}
    if current is None:
        log.warning(f"    Pattern '{pattern}' did not match current tag '{current_tag}'")
        return {}
    if len(current) < 3:
        log.warning(f"    Pattern '{pattern}' needs at least 3 capture groups (major, minor, patch), got {len(current)}")
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
    if GITHUB_TOKEN:
        log.info("GitHub token present — ghcr.io images will be checked.")
    else:
        log.info("No GITHUB_TOKEN set — ghcr.io images will be skipped.")

    containers = client.containers.list()
    log.info(f"Running containers: {len(containers)}")

    # Cache tag lists — keyed by (image_name, current_tag) so that the
    # DockerHub early-stop optimisation doesn't miss tags when two containers
    # run different versions of the same image.
    tags_cache: dict[tuple[str, str], list[str]] = {}
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

        # Fetch tags once per unique (image, tag) combination
        cache_key = (image_name, current_tag)
        if cache_key not in tags_cache:
            tags_cache[cache_key] = fetch_all_tags(image_name, token, GITHUB_TOKEN, current_tag)

        all_tags = tags_cache[cache_key]
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
