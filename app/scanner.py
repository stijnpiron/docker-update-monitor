import re
from datetime import datetime, timedelta, timezone

import docker
from docker.errors import DockerException

import app.config as _config
from app.cooldown import parse_cooldown
from app.models import UpdateInfo, RegexMismatch, ScanWarning
from app.registry import fetch_all_tags
from app.registry.dockerhub import get_dockerhub_token
from app.registry.manifest import fetch_manifest_list, is_platform_supported
from app.version import find_updates
from app.notifications import dispatch as notify
from app.state import process_scan, mark_notified
from app.health import update_state


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
    all_mismatches: list[RegexMismatch] = []
    all_warnings: list[ScanWarning] = []
    skipped_containers: list[dict] = []
    monitored_versions: dict[tuple[str, str], tuple[str, str]] = {}
    container_cooldowns: dict[str, object] = {}  # container_name → timedelta
    monitored_count = 0

    for container in containers:
        labels  = container.labels
        pattern = labels.get(f"{_config.LABEL_PREFIX}.tag-regex")

        if not pattern:
            _config.log.debug(f"  [{container.name}] No '{_config.LABEL_PREFIX}.tag-regex' label — skipping")
            # Determine image for display
            skip_image = ""
            if container.image.tags:
                skip_image = container.image.tags[0]
            else:
                skip_image = container.attrs.get("Config", {}).get("Image", "")
            skip_stack = (
                labels.get(f"{_config.LABEL_PREFIX}.stack")
                or labels.get("com.docker.compose.project")
                or "standalone"
            )
            skipped_containers.append({
                "container_name": container.name,
                "stack": skip_stack,
                "image": skip_image,
                "reason": f"No '{_config.LABEL_PREFIX}.tag-regex' label",
            })
            continue

        monitored_count += 1

        # Parse per-container cooldown label; fall back to global config
        cooldown_label = labels.get(f"{_config.LABEL_PREFIX}.update-cooldown", _config.UPDATE_COOLDOWN)
        try:
            container_cooldowns[container.name] = parse_cooldown(cooldown_label)
        except ValueError:
            _config.log.warning(
                f"  [{container.name}] Invalid update-cooldown value '{cooldown_label}' — using no cooldown"
            )
            container_cooldowns[container.name] = parse_cooldown("0")

        try:
            re.compile(pattern)
        except re.error as exc:
            msg = f"Invalid tag-regex '{pattern}': {exc}"
            _config.log.warning(f"  [{container.name}] {msg} — skipping")
            all_warnings.append(ScanWarning(
                container_name=container.name, image="", level="warning", message=msg,
            ))
            continue

        # Resolve full image reference
        image_ref = None
        if container.image.tags:
            image_ref = container.image.tags[0]
        else:
            # Fallback: read from container attrs
            image_ref = container.attrs.get("Config", {}).get("Image", "")

        if not image_ref:
            msg = "Cannot determine image reference"
            _config.log.warning(f"  [{container.name}] {msg} — skipping")
            all_warnings.append(ScanWarning(
                container_name=container.name, image="", level="warning", message=msg,
            ))
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

        # Detect stack and service (Compose sets these automatically)
        stack = (
            labels.get(f"{_config.LABEL_PREFIX}.stack")
            or labels.get("com.docker.compose.project")
            or "standalone"
        )
        service_name = labels.get("com.docker.compose.service", "")

        _config.log.info(f"  [{container.name}]  image={image_name}:{current_tag}  stack={stack}")

        # Fetch tags once per unique (image, tag) combination
        cache_key = (image_name, current_tag)
        if cache_key not in tags_cache:
            tags_cache[cache_key] = fetch_all_tags(image_name, token, _config.GITHUB_TOKEN, current_tag)

        all_tags = tags_cache[cache_key]
        if not all_tags:
            msg = f"No tags returned for {image_name}"
            _config.log.warning(f"    {msg} — skipping")
            all_warnings.append(ScanWarning(
                container_name=container.name, image=image_name, level="warning", message=msg,
            ))
            continue

        # Check if pattern matches current tag before attempting update detection
        if not re.fullmatch(pattern, current_tag):
            reason = f"Pattern '{pattern}' did not match current tag '{current_tag}'"
            _config.log.warning(f"    {reason}")
            all_mismatches.append(RegexMismatch(
                container_name=container.name,
                service_name=service_name,
                stack=stack,
                image=image_name,
                current_tag=current_tag,
                pattern=pattern,
                reason=reason,
            ))
            continue

        # Container fully validated — record its current version
        monitored_versions[(container.name, image_name)] = (current_tag, pattern)

        # Determine whether to perform architecture compatibility checks
        check_arch = labels.get(f"{_config.LABEL_PREFIX}.check-arch", "true").lower() != "false"
        container_os: str = ""
        container_arch: str = ""
        if check_arch:
            try:
                image_attrs = container.image.attrs or {}
                container_os = image_attrs.get("Os", "") or ""
                container_arch = image_attrs.get("Architecture", "") or ""
                if not container_os or not container_arch:
                    _config.log.warning(
                        f"  [{container.name}] Platform info unavailable from Docker API"
                        " — skipping arch check"
                    )
                    check_arch = False
            except Exception as exc:
                _config.log.warning(
                    f"  [{container.name}] Could not read platform info: {exc}"
                    " — skipping arch check"
                )
                check_arch = False

        updates = find_updates(current_tag, all_tags, pattern)

        if not updates:
            _config.log.info(f"    No updates found (current={current_tag})")
        else:
            for update_type, new_tag in updates.items():
                # Check architecture compatibility before reporting the update
                if check_arch and container_os and container_arch:
                    platforms = fetch_manifest_list(
                        image_name, new_tag,
                        _config.DOCKERHUB_USER, _config.DOCKERHUB_PASS,
                        _config.GITHUB_TOKEN,
                    )
                    if not is_platform_supported(platforms, container_os, container_arch):
                        _config.log.info(
                            f"    Skipping {update_type.upper()} update {current_tag} → {new_tag}:"
                            f" tag '{new_tag}' does not support"
                            f" {container_os}/{container_arch}"
                        )
                        continue

                _config.log.info(f"    {update_type.upper():5s} update: {current_tag} → {new_tag}")
                all_updates.append(UpdateInfo(
                    container_name=container.name,
                    service_name=service_name,
                    stack=stack,
                    image=image_name,
                    current_version=current_tag,
                    new_version=new_tag,
                    update_type=update_type,
                ))

    _config.log.info("-" * 60)
    _config.log.info(f"Check complete — {len(all_updates)} update(s) detected")
    if all_mismatches:
        _config.log.info(f"  Regex mismatches: {len(all_mismatches)}")
    if all_warnings:
        _config.log.info(f"  Warnings: {len(all_warnings)}")

    scan_time = datetime.now(timezone.utc)

    # Persist state and categorize: new / known / resolved
    categorized = process_scan(all_updates, scan_time, current_versions=monitored_versions)

    # Deduplicate: keep only the highest new_version per (container, image, update_type).
    # The DB unique constraint already prevents exact duplicates; this guards against
    # any edge case where the same container+image+type appears with different new_versions.
    _seen: dict[tuple[str, str, str], str] = {}
    _deduped: list[UpdateInfo] = []
    for _u in categorized:
        _key = (_u.container_name, _u.image, _u.update_type)
        if _key not in _seen or (_u.new_version or "") > _seen[_key]:
            _seen[_key] = _u.new_version or ""
            _deduped.append(_u)
    categorized = _deduped

    new_count = sum(1 for u in categorized if u.status == "new")
    known_count = sum(1 for u in categorized if u.status == "known")
    resolved_count = sum(1 for u in categorized if u.status == "resolved")
    _config.log.info(f"  New: {new_count}  |  Known: {known_count}  |  Resolved: {resolved_count}")

    # Apply cooldown — suppress new/known updates that haven't matured yet
    global_cooldown = parse_cooldown(_config.UPDATE_COOLDOWN)
    actionable: list[UpdateInfo] = []
    for u in categorized:
        if u.status == "resolved":
            actionable.append(u)
            continue
        cooldown = container_cooldowns.get(u.container_name, global_cooldown)
        if cooldown and u.first_seen_at:
            first_seen = datetime.fromisoformat(u.first_seen_at)
            if scan_time - first_seen < cooldown:
                _config.log.info(
                    f"  [{u.container_name}] {u.current_version} → {u.new_version} "
                    f"in cooldown ({cooldown}), skipping notification"
                )
                continue
        actionable.append(u)

    # Notify with all categorized updates (grouped by status in payload)
    notify(actionable, mismatches=all_mismatches, warnings=all_warnings)
    if actionable:
        mark_notified(actionable, scan_time)

    # Update health endpoint state
    warnings_data = [
        {"container_name": w.container_name, "image": w.image, "level": w.level, "message": w.message}
        for w in all_warnings
    ] + [
        {"container_name": m.container_name, "image": m.image, "level": "warning", "message": m.reason}
        for m in all_mismatches
    ]
    update_state(last_check=scan_time, containers_monitored=monitored_count,
                 warnings=warnings_data, skipped_containers=skipped_containers)
