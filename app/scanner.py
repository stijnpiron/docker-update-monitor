import re
import time
from datetime import datetime, timedelta, timezone

_GIT_HASH_RE = re.compile(r"sha-[a-f0-9]{7,}")

import docker
import requests
from docker.errors import APIError as DockerAPIError, DockerException

import app.config as _config
from app.cooldown import parse_cooldown
from app.models import UpdateInfo, RegexMismatch, ScanWarning
from app.registry import fetch_all_tags
from app.registry.dockerhub import get_dockerhub_token
from app.registry.manifest import fetch_manifest_list, is_platform_supported, fetch_digest, fetch_platform_digest
from app.version import find_updates
from app.notifications import dispatch as notify
from app.state import process_scan, mark_notified, get_stored_digest, store_digest, get_all_updates
from app.health import update_state
from app.metrics import check_errors_total, update_after_scan


def _is_higher_version(candidate: str | None, current: str | None) -> bool:
    """Return True if candidate > current using semver-aware (integer) comparison.

    Splits both strings by '.' and compares segment-by-segment as integers.
    Falls back to plain string comparison when any segment is non-numeric
    (e.g. digest hashes), where string ordering is an acceptable approximation.
    """
    c = candidate or ""
    b = current or ""
    try:
        return tuple(int(p) for p in c.split(".")) > tuple(int(p) for p in b.split("."))
    except ValueError:
        return c > b


def _extract_local_digest(repo_digests: list[str]) -> str | None:
    """Extract the first sha256 digest from a Docker RepoDigests list.

    RepoDigests entries use the format "image@sha256:digest".
    Returns the digest portion (e.g. "sha256:abc123...") of the first valid entry.
    """
    for entry in repo_digests:
        if "@" in entry:
            _, digest = entry.split("@", 1)
            if digest.startswith("sha256:"):
                return digest
    return None


def _resolve_digest_to_tag(
    image_name: str,
    target_digest: str,
    all_tags: list[str],
    pattern: str,
    current_tag: str = "",
) -> str | None:
    """Find which tag shares the same digest as target_digest.

    First tries tags matching the version pattern (fast path for semver images).
    Falls back to non-pattern, non-current tags when no versioned tag matches —
    covers git-hash tags (e.g. sha-675e77e) used by GHCR and Docker Hub.
    The pattern defines what is "versioned"; everything else is treated as rolling.
    """
    matching_tags = [t for t in all_tags if t != current_tag and re.fullmatch(pattern, t)]
    for tag in matching_tags:
        tag_digest = fetch_digest(
            image_name, tag,
            _config.DOCKERHUB_USER, _config.DOCKERHUB_PASS,
            _config.GITHUB_TOKEN,
        )
        if tag_digest == target_digest:
            return tag

    # Fallback: check non-pattern, non-current tags (rolling tags like sha-XXXXXXX).
    # Prefer git-hash style tags first; cap at 20 to limit extra API calls.
    fallback = [
        t for t in all_tags
        if t != current_tag and not re.fullmatch(pattern, t)
    ]
    fallback.sort(key=lambda t: (0 if _GIT_HASH_RE.fullmatch(t) else 1, t))
    for tag in fallback[:20]:
        tag_digest = fetch_digest(
            image_name, tag,
            _config.DOCKERHUB_USER, _config.DOCKERHUB_PASS,
            _config.GITHUB_TOKEN,
        )
        if tag_digest == target_digest:
            return tag

    return None


def run_check() -> None:
    _config.log.info("=" * 60)
    _config.log.info("Starting update check")
    _config.log.info("=" * 60)

    _scan_start = time.monotonic()

    try:
        client = docker.from_env()
    except DockerException as exc:
        _config.log.error(f"Cannot connect to Docker: {exc}")
        check_errors_total.inc()
        return

    try:
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
        running_digests: dict[tuple[str, str], list[str]] = {}
        container_cooldowns: dict[str, timedelta] = {}
        monitored_count = 0

        for container in containers:
            container_name: str = container.name or ""
            labels  = container.labels
            mode    = labels.get(f"{_config.LABEL_PREFIX}.mode", "").lower()
            pattern = labels.get(f"{_config.LABEL_PREFIX}.tag-regex")

            if not pattern and mode != "digest":
                _config.log.debug(f"  [{container_name}] No '{_config.LABEL_PREFIX}.tag-regex' label — skipping")
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
                    "container_name": container_name,
                    "stack": skip_stack,
                    "image": skip_image,
                    "reason": f"No '{_config.LABEL_PREFIX}.tag-regex' label",
                })
                continue

            monitored_count += 1

            # Parse per-container cooldown label; fall back to global config
            cooldown_label = labels.get(f"{_config.LABEL_PREFIX}.update-cooldown", _config.UPDATE_COOLDOWN)
            try:
                container_cooldowns[container_name] = parse_cooldown(cooldown_label)
            except ValueError:
                _config.log.warning(
                    f"  [{container_name}] Invalid update-cooldown value '{cooldown_label}' — using no cooldown"
                )
                container_cooldowns[container_name] = parse_cooldown("0")

            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    msg = f"Invalid tag-regex '{pattern}': {exc}"
                    _config.log.warning(f"  [{container_name}] {msg} — skipping")
                    all_warnings.append(ScanWarning(
                        container_name=container_name, image="", level="warning", message=msg,
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
                _config.log.warning(f"  [{container_name}] {msg} — skipping")
                all_warnings.append(ScanWarning(
                    container_name=container_name, image="", level="warning", message=msg,
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

            _config.log.info(f"  [{container_name}]  image={image_name}:{current_tag}  stack={stack}")

            if mode == "digest":
                # Explicit digest mode: compare the local running image digest (RepoDigests)
                # against the remote registry digest via HEAD request.  Works on first scan —
                # no need for a silent storage phase.
                repo_digests = container.image.attrs.get("RepoDigests") or []
                local_digest = _extract_local_digest(repo_digests)

                if not local_digest:
                    msg = f"No RepoDigests available for {image_name}:{current_tag} — cannot compare"
                    _config.log.warning(f"    {msg}")
                    all_warnings.append(ScanWarning(
                        container_name=container_name, image=image_name, level="warning", message=msg,
                    ))
                    monitored_versions[(container_name, image_name)] = (current_tag, pattern or "")
                    running_digests[(container_name, image_name)] = repo_digests
                    continue

                remote_digest = fetch_digest(
                    image_name, current_tag,
                    _config.DOCKERHUB_USER, _config.DOCKERHUB_PASS,
                    _config.GITHUB_TOKEN,
                )
                if not remote_digest:
                    msg = f"Could not fetch remote digest for {image_name}:{current_tag}"
                    _config.log.warning(f"    {msg}")
                    all_warnings.append(ScanWarning(
                        container_name=container_name, image=image_name, level="warning", message=msg,
                    ))
                    monitored_versions[(container_name, image_name)] = (current_tag, pattern or "")
                    running_digests[(container_name, image_name)] = repo_digests
                    continue

                if local_digest == remote_digest:
                    _config.log.info(f"    Digest up to date ({local_digest[:19]})")
                else:
                    # Multi-arch: the manifest list digest may differ from the platform-specific
                    # digest stored in RepoDigests.  Check whether the platform-specific digest
                    # is unchanged before reporting an update.
                    platform_match = False
                    try:
                        image_attrs = container.image.attrs or {}
                        container_os = image_attrs.get("Os", "") or ""
                        container_arch = image_attrs.get("Architecture", "") or ""
                        if container_os and container_arch:
                            platform_digest = fetch_platform_digest(
                                image_name, current_tag,
                                container_os, container_arch,
                                _config.DOCKERHUB_USER, _config.DOCKERHUB_PASS,
                                _config.GITHUB_TOKEN,
                            )
                            if platform_digest and platform_digest == local_digest:
                                platform_match = True
                                _config.log.info(
                                    f"    Platform digest unchanged for"
                                    f" {container_os}/{container_arch} ({local_digest[:19]})"
                                )
                    except (DockerAPIError, KeyError, requests.RequestException) as exc:
                        _config.log.debug(f"    Could not check platform digest: {exc}")

                    if not platform_match:
                        _config.log.info(
                            f"    Digest changed: {local_digest[:19]} → {remote_digest[:19]}"
                        )
                        # Optionally resolve the new digest to a versioned tag
                        resolved_version = None
                        if pattern:
                            cache_key = (image_name, current_tag)
                            if cache_key not in tags_cache:
                                tags_cache[cache_key] = fetch_all_tags(
                                    image_name, token, _config.GITHUB_TOKEN, current_tag
                                )
                            all_tags = tags_cache[cache_key]
                            if all_tags:
                                resolved_version = _resolve_digest_to_tag(
                                    image_name, remote_digest, all_tags, pattern,
                                    current_tag=current_tag,
                                )
                        new_version = resolved_version or remote_digest
                        all_updates.append(UpdateInfo(
                            container_name=container_name,
                            service_name=service_name,
                            stack=stack,
                            image=image_name,
                            current_version=current_tag,
                            new_version=new_version,
                            update_type="digest",
                        ))

                monitored_versions[(container_name, image_name)] = (current_tag, pattern or "")
                running_digests[(container_name, image_name)] = repo_digests
                continue

            # Fetch tags once per unique (image, tag) combination
            cache_key = (image_name, current_tag)
            if cache_key not in tags_cache:
                tags_cache[cache_key] = fetch_all_tags(image_name, token, _config.GITHUB_TOKEN, current_tag)

            all_tags = tags_cache[cache_key]
            if not all_tags:
                msg = f"No tags returned for {image_name}"
                _config.log.warning(f"    {msg} — skipping")
                all_warnings.append(ScanWarning(
                    container_name=container_name, image=image_name, level="warning", message=msg,
                ))
                continue

            # Check if pattern matches current tag before attempting update detection
            if not re.fullmatch(pattern, current_tag):
                # Digest-based detection: current tag doesn't match the version pattern
                _config.log.info(f"    Tag '{current_tag}' does not match pattern — using digest mode")

                current_digest = fetch_digest(
                    image_name, current_tag,
                    _config.DOCKERHUB_USER, _config.DOCKERHUB_PASS,
                    _config.GITHUB_TOKEN,
                )
                if not current_digest:
                    msg = f"Could not fetch digest for {image_name}:{current_tag}"
                    _config.log.warning(f"    {msg} — skipping")
                    all_warnings.append(ScanWarning(
                        container_name=container_name, image=image_name,
                        level="warning", message=msg,
                    ))
                    continue

                stored_digest = get_stored_digest(image_name, current_tag)

                if stored_digest is None:
                    # First scan — silently store the digest, no notification
                    _config.log.info(f"    First scan — storing digest {current_digest[:19]}")
                    store_digest(image_name, current_tag, current_digest)
                elif current_digest == stored_digest:
                    _config.log.info(f"    Digest unchanged ({current_digest[:19]})")
                else:
                    # Digest changed — resolve to a versioned tag
                    _config.log.info(f"    Digest changed: {stored_digest[:19]} → {current_digest[:19]}")

                    # Try to find which versioned tag matches the new digest
                    resolved_version = _resolve_digest_to_tag(
                        image_name, current_digest, all_tags, pattern,
                        current_tag=current_tag,
                    )

                    if resolved_version:
                        new_version = resolved_version
                        _config.log.info(f"    Resolved: {current_tag} → {new_version}")
                    else:
                        # Fallback to full digest — usable as image@sha256:... reference
                        new_version = current_digest
                        _config.log.info(f"    Could not resolve to tag — using digest {new_version[:19]}")

                    all_updates.append(UpdateInfo(
                        container_name=container_name,
                        service_name=service_name,
                        stack=stack,
                        image=image_name,
                        current_version=current_tag,
                        new_version=new_version,
                        update_type="digest",
                    ))

                    # Update stored digest
                    store_digest(image_name, current_tag, current_digest)

                # Track digest-mode containers so stale entries can be cleaned up
                # when the rolling tag changes (e.g. :edge → :dev), and so that
                # auto-resolution can compare against the running image's RepoDigests.
                monitored_versions[(container_name, image_name)] = (current_tag, pattern)
                running_digests[(container_name, image_name)] = (
                    container.image.attrs.get("RepoDigests") or []
                )
                continue

            # Container fully validated — record its current version
            monitored_versions[(container_name, image_name)] = (current_tag, pattern)

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
                            f"  [{container_name}] Platform info unavailable from Docker API"
                            " — skipping arch check"
                        )
                        check_arch = False
                except Exception as exc:
                    _config.log.warning(
                        f"  [{container_name}] Could not read platform info: {exc}"
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
                        container_name=container_name,
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
        categorized = process_scan(
            all_updates, scan_time,
            current_versions=monitored_versions,
            running_digests=running_digests,
        )

        # Deduplicate: keep only the highest new_version per (container, image, update_type).
        # The DB unique constraint already prevents exact duplicates; this guards against
        # any edge case where the same container+image+type appears with different new_versions.
        _deduped: dict[tuple[str, str, str], UpdateInfo] = {}
        for _u in categorized:
            _key = (_u.container_name, _u.image, _u.update_type)
            if _key not in _deduped or _is_higher_version(_u.new_version, _deduped[_key].new_version):
                _deduped[_key] = _u
        categorized = list(_deduped.values())

        new_count = sum(1 for u in categorized if u.status == "new")
        known_count = sum(1 for u in categorized if u.status == "known")
        resolved_count = sum(1 for u in categorized if u.status == "resolved")
        _config.log.info(f"  New: {new_count}  |  Known: {known_count}  |  Resolved: {resolved_count}")

        # Build the notification payload — pending (new/known) updates only.
        # Resolved updates are informational; they are persisted in the DB and
        # surfaced by the web dashboard, but notifications are for actionable
        # (still-pending) updates, so they're excluded here.
        # Also apply cooldown — suppress new/known updates that haven't matured yet.
        global_cooldown = parse_cooldown(_config.UPDATE_COOLDOWN)
        actionable: list[UpdateInfo] = []
        for u in categorized:
            if u.status == "resolved":
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

        # Update Prometheus metrics
        if all_warnings:
            check_errors_total.inc(len(all_warnings))
        update_after_scan(
            monitored=monitored_count,
            updates=get_all_updates(),
            duration_seconds=time.monotonic() - _scan_start,
            last_check_ts=scan_time.timestamp(),
        )

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
    finally:
        client.close()
