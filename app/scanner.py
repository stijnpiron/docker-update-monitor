import re
from datetime import datetime, timezone

import docker
from docker.errors import DockerException

import app.config as _config
from app.models import UpdateInfo
from app.registry import fetch_all_tags
from app.registry.dockerhub import get_dockerhub_token
from app.version import find_updates
from app.notifications import dispatch as notify
from app.state import process_scan, mark_notified


def run_check() -> None:
    _config.log.info("=" * 60)
    _config.log.info("Starting update check")
    _config.log.info("=" * 60)

    try:
        client = docker.from_env()
    except DockerException as exc:
        _config.log.error(f"Cannot connect to Docker: {exc}")
        return

    token = get_dockerhub_token(_config.DOCKERHUB_USER, _config.DOCKERHUB_PASS)
    if _config.GITHUB_TOKEN:
        _config.log.info("GitHub token present — ghcr.io images will be checked.")
    else:
        _config.log.info("No GITHUB_TOKEN set — ghcr.io images will be skipped.")

    containers = client.containers.list()
    _config.log.info(f"Running containers: {len(containers)}")

    # Cache tag lists — keyed by (image_name, current_tag) so that the
    # DockerHub early-stop optimisation doesn't miss tags when two containers
    # run different versions of the same image.
    tags_cache: dict[tuple[str, str], list[str]] = {}
    all_updates: list[UpdateInfo] = []

    for container in containers:
        labels  = container.labels
        pattern = labels.get(f"{_config.LABEL_PREFIX}.tag-regex")

        if not pattern:
            _config.log.debug(f"  [{container.name}] No '{_config.LABEL_PREFIX}.tag-regex' label — skipping")
            continue

        try:
            re.compile(pattern)
        except re.error as exc:
            _config.log.warning(f"  [{container.name}] Invalid tag-regex '{pattern}': {exc} — skipping")
            continue

        # Resolve full image reference
        image_ref = None
        if container.image.tags:
            image_ref = container.image.tags[0]
        else:
            # Fallback: read from container attrs
            image_ref = container.attrs.get("Config", {}).get("Image", "")

        if not image_ref:
            _config.log.warning(f"  [{container.name}] Cannot determine image reference — skipping")
            continue

        # Split into name + tag
        # Handle registry prefixes: registry.example.com:5000/ns/image:tag
        # Strategy: split on the last colon that follows a slash (or the only colon if no slashes after it)

        # Strip digest suffix if present
        if "@" in image_ref:
            image_ref = image_ref.split("@")[0]

        if ":" in image_ref.split("/")[-1]:
            image_name, current_tag = image_ref.rsplit(":", 1)
        else:
            image_name, current_tag = image_ref, "latest"

        # Detect stack (Compose sets this automatically)
        stack = (
            labels.get(f"{_config.LABEL_PREFIX}.stack")
            or labels.get("com.docker.compose.project")
            or "standalone"
        )

        _config.log.info(f"  [{container.name}]  image={image_name}:{current_tag}  stack={stack}")

        # Fetch tags once per unique (image, tag) combination
        cache_key = (image_name, current_tag)
        if cache_key not in tags_cache:
            tags_cache[cache_key] = fetch_all_tags(image_name, token, _config.GITHUB_TOKEN, current_tag)

        all_tags = tags_cache[cache_key]
        if not all_tags:
            _config.log.warning(f"    No tags returned for {image_name} — skipping")
            continue

        updates = find_updates(current_tag, all_tags, pattern)

        if not updates:
            _config.log.info(f"    No updates found (current={current_tag})")
        else:
            for update_type, new_tag in updates.items():
                _config.log.info(f"    {update_type.upper():5s} update: {current_tag} → {new_tag}")
                all_updates.append(UpdateInfo(
                    container_name=container.name,
                    stack=stack,
                    image=image_name,
                    current_version=current_tag,
                    new_version=new_tag,
                    update_type=update_type,
                ))

    _config.log.info("-" * 60)
    _config.log.info(f"Check complete — {len(all_updates)} update(s) detected")

    scan_time = datetime.now(timezone.utc)

    # Persist state and categorize: new / known / resolved
    categorized = process_scan(all_updates, scan_time)

    new_count = sum(1 for u in categorized if u.status == "new")
    known_count = sum(1 for u in categorized if u.status == "known")
    resolved_count = sum(1 for u in categorized if u.status == "resolved")
    _config.log.info(f"  New: {new_count}  |  Known: {known_count}  |  Resolved: {resolved_count}")

    # Notify with all categorized updates (grouped by status in payload)
    notify(categorized)
    if categorized:
        mark_notified(categorized, scan_time)
