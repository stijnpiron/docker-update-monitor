import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_GIT_HASH_RE = re.compile(r"sha-[a-f0-9]{7,}")

import docker
import requests
from docker.errors import APIError as DockerAPIError, DockerException

import app.config as _config
from app.cooldown import parse_cooldown
from app.models import UpdateInfo, RegexMismatch, ScanWarning, HostStatusEvent
from app.registry import fetch_all_tags
from app.registry.dockerhub import get_dockerhub_token
from app.registry.manifest import fetch_manifest_list, is_platform_supported, fetch_digest, fetch_platform_digest
from app.version import find_updates
from app.notifications import dispatch as notify
from app.notifications.host_status import notify_host_status
from app.state import (
    process_scan, mark_notified, get_stored_digest, store_digest, get_all_updates,
    get_host_status, upsert_host_status,
)
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


# Bounded per-request socket timeout (seconds) for remote (SSH) Docker clients,
# so an unresponsive host fails fast as unreachable instead of stalling the
# whole check.
_HOST_CLIENT_TIMEOUT = 30

# Last-seen reachability per host, carried across scans so that down/up
# transitions can be detected (task 05 raises the up/down alerts from this).
_last_host_reachable: dict[str, bool] = {}
# Last error string for each host, so a freshly-detected down event can carry
# the reason into its HostStatusEvent.
_last_host_errors: dict[str, str] = {}


@dataclass
class _HostScanResult:
    """Raw per-host artifacts from one pass over a single Docker daemon.

    No DB writes happen here — ``run_check`` feeds each host's raw results into
    ``process_scan`` separately so state cleanup stays scoped per host.
    """
    host: str
    raw_updates: list[UpdateInfo]
    mismatches: list[RegexMismatch] = field(default_factory=list)
    warnings: list[ScanWarning] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    monitored_versions: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    running_digests: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    existing_containers: set[str] | None = None
    container_cooldowns: dict[str, timedelta] = field(default_factory=dict)
    monitored_count: int = 0


def _scan_host(host: str, client, token: str | None) -> _HostScanResult:
    """Scan one Docker daemon and return its raw artifacts.

    All in-memory scan state (``tags_cache``, ``monitored_versions``,
    ``running_digests``, ``container_cooldowns``, ``existing_containers``) is
    local to this call, so same-named containers on different hosts are fully
    isolated.
    """
    containers = client.containers.list()
    if host == "local":
        _config.log.info(f"Running containers: {len(containers)}")
    else:
        _config.log.info(f"[{host}] Running containers: {len(containers)}")

    # Full set of container names Docker currently knows about (running and
    # stopped).  Used to distinguish a *removed* container — whose pending
    # update should be dropped — from one that merely failed to scan this
    # cycle.  If the listing fails, fall back to None so removal cleanup is
    # skipped rather than risking deletion on incomplete data. Scoped to
    # this host — a container removed on one host says nothing about
    # another.
    try:
        existing_containers: set[str] | None = {
            c.name or "" for c in client.containers.list(all=True)
        }
    except DockerException as exc:
        _config.log.warning(f"Could not list all containers: {exc} — skipping removal cleanup")
        existing_containers = None

    # Cache tag lists — keyed by (image_name, current_tag) so that the
    # DockerHub early-stop optimisation doesn't miss tags when two containers
    # run different versions of the same image. Per host.
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

        # Resolve the container's image once. Accessing container.image lazily
        # calls the Docker API (images.get); if the underlying image was pruned
        # or removed out from under a still-listed container it raises
        # ImageNotFound. Contain that failure here so a single broken container
        # is reported but can't crash the whole scan (fix #163).
        try:
            image_obj = container.image
        except (DockerException, requests.RequestException) as exc:
            image_hint = container.attrs.get("Config", {}).get("Image", "") or ""
            msg = f"Could not inspect image: {exc}"
            _config.log.error(f"  [{container_name}] {msg} — skipping")
            all_warnings.append(ScanWarning(
                container_name=container_name, image=image_hint,
                level="error", message=msg, host=host,
            ))
            continue

        mode    = labels.get(f"{_config.LABEL_PREFIX}.mode", "").lower()
        pattern = labels.get(f"{_config.LABEL_PREFIX}.tag-regex")

        if not pattern and mode != "digest":
            _config.log.debug(f"  [{container_name}] No '{_config.LABEL_PREFIX}.tag-regex' label — skipping")
            # Determine image for display
            skip_image = ""
            if image_obj.tags:
                skip_image = image_obj.tags[0]
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
                "host": host,
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
                    host=host,
                ))
                continue

        # Resolve full image reference
        image_ref = None
        if image_obj.tags:
            image_ref = image_obj.tags[0]
        else:
            # Fallback: read from container attrs
            image_ref = container.attrs.get("Config", {}).get("Image", "")

        if not image_ref:
            msg = "Cannot determine image reference"
            _config.log.warning(f"  [{container_name}] {msg} — skipping")
            all_warnings.append(ScanWarning(
                host=host,
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
            repo_digests = image_obj.attrs.get("RepoDigests") or []
            local_digest = _extract_local_digest(repo_digests)

            if not local_digest:
                msg = f"No RepoDigests available for {image_name}:{current_tag} — cannot compare"
                _config.log.warning(f"    {msg}")
                all_warnings.append(ScanWarning(
                    container_name=container_name, image=image_name, level="warning", message=msg,
                    host=host,
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
                    host=host,
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
                    image_attrs = image_obj.attrs or {}
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
                        host=host,
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
                    level="warning", message=msg, host=host,
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
                    host=host,
                ))

                # Update stored digest
                store_digest(image_name, current_tag, current_digest)

            # Track digest-mode containers so stale entries can be cleaned up
            # when the rolling tag changes (e.g. :edge → :dev), and so that
            # auto-resolution can compare against the running image's RepoDigests.
            monitored_versions[(container_name, image_name)] = (current_tag, pattern)
            running_digests[(container_name, image_name)] = (
                image_obj.attrs.get("RepoDigests") or []
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
                image_attrs = image_obj.attrs or {}
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
                    host=host,
                    update_type=update_type,
                ))

    _config.log.info("-" * 60)
    _config.log.info(f"Check complete — {len(all_updates)} update(s) detected")
    if all_mismatches:
        _config.log.info(f"  Regex mismatches: {len(all_mismatches)}")
    if all_warnings:
        _config.log.info(f"  Warnings: {len(all_warnings)}")

    return _HostScanResult(
        host=host,
        raw_updates=all_updates,
        mismatches=all_mismatches,
        warnings=all_warnings,
        skipped=skipped_containers,
        monitored_versions=monitored_versions,
        running_digests=running_digests,
        existing_containers=existing_containers,
        container_cooldowns=container_cooldowns,
        monitored_count=monitored_count,
    )


def _connect_host(host: str, url: str | None):
    """Open a Docker client for the given host.

    ``url is None`` → the local daemon (docker.from_env); otherwise an SSH
    client with a bounded socket timeout so an unresponsive host fails fast as
    unreachable instead of stalling the check. Raises ``DockerException`` on
    connection failure.
    """
    if url is None:
        return docker.from_env()
    _config.log.info(f"[{host}] Connecting to {url} (timeout={_HOST_CLIENT_TIMEOUT}s)")
    return docker.DockerClient(base_url=url, use_ssh_client=True,
                               timeout=_HOST_CLIENT_TIMEOUT)


def _record_host_reachable(host: str) -> None:
    """Record *host* as reachable and note the transition in memory."""
    upsert_host_status(host, True, None, datetime.now(timezone.utc).isoformat())
    _last_host_reachable[host] = True
    _last_host_errors.pop(host, None)


def _record_host_unreachable(host: str, error: str) -> None:
    """Record *host* as unreachable and note the transition in memory."""
    upsert_host_status(host, False, error, datetime.now(timezone.utc).isoformat())
    _last_host_reachable[host] = False
    _last_host_errors[host] = error
    check_errors_total.inc()


def _detect_host_events(before: dict[str, bool], after: dict[str, bool]) -> list[HostStatusEvent]:
    """Compare per-host reachability before/after a scan and return transitions.

    * ``down``      — a host that was reachable on the previous scan is now
      unreachable (``True → False``).
    * ``recovered`` — a host that was unreachable on the previous scan is now
      reachable (``False → True``).

    A host whose *prior* state is missing (``None``) is never turned into an
    event: this is the first-scan priming rule — on the very first scan (or the
    first scan after a host is added) there is no baseline to compare against,
    so nothing is alerted (design doc "Edge cases", task 05 AC6). Coalescing of
    repeated transitions is left to ``notify_host_status`` (task 05 AC3/AC5).
    """
    events: list[HostStatusEvent] = []
    for host, now_reachable in after.items():
        prev = before.get(host)
        if prev is None:
            continue  # first scan for this host — no baseline, no alert
        if prev and not now_reachable:
            events.append(HostStatusEvent(
                host=host, event="down", error=_last_host_errors.get(host),
            ))
        elif (not prev) and now_reachable:
            events.append(HostStatusEvent(host=host, event="recovered"))
    return events


def run_check() -> None:
    _config.log.info("=" * 60)
    _config.log.info("Starting update check")
    _config.log.info("=" * 60)

    _scan_start = time.monotonic()

    try:
        token = get_dockerhub_token(_config.DOCKERHUB_USER, _config.DOCKERHUB_PASS)
        if _config.GITHUB_TOKEN:
            _config.log.info("GitHub token present — ghcr.io images will be checked.")
        else:
            _config.log.info("No GITHUB_TOKEN set — ghcr.io images will be skipped.")
    except Exception as exc:
        _config.log.error(f"Cannot obtain DockerHub token: {exc}")
        check_errors_total.inc()
        return

    hosts = _config.DOCKER_HOSTS

    # Baseline reachability from the previous scan, used to detect
    # down/recovered transitions this cycle (task 05).
    host_reachable_before = dict(_last_host_reachable)

    # Per-host raw scan artifacts; hosts that raise are recorded in
    # host_status below and simply absent from this list.
    results: list[_HostScanResult] = []

    for host, url in hosts:
        hp = f"[{host}] " if host != "local" else ""
        try:
            client = _connect_host(host, url)
        except DockerException as exc:
            _config.log.warning(f"{hp}Unreachable — {exc}")
            if host == "local" and len(hosts) == 1:
                # Preserve the pre-feature single-local-host behavior: a
                # connection failure is logged as the fatal error and ends
                # the check.
                _config.log.error("Cannot connect to Docker")
                check_errors_total.inc()
                return
            _record_host_unreachable(host, str(exc))
            continue

        try:
            result = _scan_host(host, client, token)
            results.append(result)
            _record_host_reachable(host)
        except (DockerException, requests.RequestException) as exc:
            _config.log.warning(f"{hp}Unreachable — {exc}")
            _record_host_unreachable(host, str(exc))
        finally:
            # Always close — remote clients hold SSH connections that must
            # not leak across repeated scans.
            client.close()

    # Host up/down alerts are independent of the update payload: a reachability
    # transition must be reported even when no host produced any scan results
    # this cycle. Coalescing and DRY_RUN handling live in notify_host_status
    # (task 05); transitions are first-scan-primed via _detect_host_events.
    host_events = _detect_host_events(host_reachable_before, _last_host_reachable)
    if host_events:
        notify_host_status(host_events)

    if not results:
        _config.log.error("No hosts were reachable in this scan cycle")
        return

    scan_time = datetime.now(timezone.utc)

    # Persist per host: process_scan is called once per host so resolution and
    # remove-container cleanup stay scoped to each host (container names are
    # not unique across hosts).
    all_categorized: list[UpdateInfo] = []
    all_mismatches: list[RegexMismatch] = []
    all_warnings: list[ScanWarning] = []
    skipped_containers: list[dict] = []
    container_cooldowns: dict[str, timedelta] = {}
    monitored_total = 0

    for r in results:
        hp = f"[{r.host}] " if r.host != "local" else ""
        categorized = process_scan(
            r.raw_updates, scan_time,
            current_versions=r.monitored_versions,
            running_digests=r.running_digests,
            existing_containers=r.existing_containers,
            host=r.host,
        )

        # Deduplicate: keep only the highest new_version per
        # (host, container, image, update_type). The DB unique constraint
        # already prevents exact duplicates; this guards against any edge case
        # where the same host+container+image+type appears with different
        # new_versions. ``host`` in the key keeps same-named containers on
        # different hosts as separate entries.
        _deduped: dict[tuple[str, str, str, str], UpdateInfo] = {}
        for _u in categorized:
            _key = (_u.host, _u.container_name, _u.image, _u.update_type)
            if _key not in _deduped or _is_higher_version(_u.new_version, _deduped[_key].new_version):
                _deduped[_key] = _u
        all_categorized.extend(_deduped.values())

        all_mismatches.extend(r.mismatches)
        all_warnings.extend(r.warnings)
        skipped_containers.extend(r.skipped)
        container_cooldowns.update(r.container_cooldowns)
        monitored_total += r.monitored_count

        new_count = sum(1 for u in categorized if u.status == "new")
        known_count = sum(1 for u in categorized if u.status == "known")
        resolved_count = sum(1 for u in categorized if u.status == "resolved")
        _config.log.info(f"{hp}New: {new_count}  |  Known: {known_count}  |  Resolved: {resolved_count}")

    # Build the notification payload — pending (new/known) updates only.
    # Resolved updates are informational; they are persisted in the DB and
    # surfaced by the web dashboard, but notifications are for actionable
    # (still-pending) updates, so they're excluded here.
    # Also apply cooldown — suppress new/known updates that haven't matured yet.
    global_cooldown = parse_cooldown(_config.UPDATE_COOLDOWN)
    actionable: list[UpdateInfo] = []
    for u in all_categorized:
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

    # Single notification dispatch per scan, with the merged, host-tagged lists
    notify(actionable, mismatches=all_mismatches, warnings=all_warnings)
    if actionable:
        mark_notified(actionable, scan_time)

    # Update Prometheus metrics
    if all_warnings:
        check_errors_total.inc(len(all_warnings))
    update_after_scan(
        monitored=monitored_total,
        updates=get_all_updates(),
        duration_seconds=time.monotonic() - _scan_start,
        last_check_ts=scan_time.timestamp(),
    )

    # Update health endpoint state (host_status feeds the dashboard strip,
    # task 06). Warning/mismatch rows carry the host so the dashboard can
    # badge + sort them.
    warnings_data = [
        {"container_name": w.container_name, "image": w.image, "level": w.level,
         "message": w.message, "host": w.host, "stack": ""}
        for w in all_warnings
    ] + [
        {"container_name": m.container_name, "image": m.image, "level": "warning",
         "message": m.reason, "host": m.host, "stack": m.stack}
        for m in all_mismatches
    ]
    update_state(last_check=scan_time, containers_monitored=monitored_total,
                 warnings=warnings_data, skipped_containers=skipped_containers,
                 host_status=get_host_status())
