from dataclasses import dataclass, field


@dataclass
class UpdateInfo:
    container_name: str
    service_name: str
    stack: str
    image: str
    current_version: str
    new_version: str
    update_type: str        # "patch" | "minor" | "major" | "digest"
    host: str = "local"     # which host this update belongs to (see DOCKER_HOSTS)
    status: str = ""        # "new" | "known" | "resolved"
    first_seen_at: str | None = None


@dataclass
class HostStatus:
    # Reachability snapshot for a single host, recorded each scan cycle.
    host: str
    reachable: bool
    # Populated only when reachable is False (by convention, not the type).
    error: str | None
    # ISO-8601 UTC, same convention as other timestamps in the project.
    checked_at: str


@dataclass
class RegexMismatch:
    container_name: str
    service_name: str
    stack: str
    image: str
    current_tag: str
    pattern: str
    reason: str             # e.g. "did not match current tag"
    host: str = "local"     # which host this mismatch belongs to (see DOCKER_HOSTS)


@dataclass
class ScanWarning:
    container_name: str
    image: str
    level: str              # "warning" | "error"
    message: str
    host: str = "local"     # which host this warning belongs to (see DOCKER_HOSTS)
