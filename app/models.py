from dataclasses import dataclass


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
class HostStatusEvent:
    """A host reachability transition detected between two scan cycles.

    Not an ``UpdateInfo`` — host up/down alerts are not container update
    rows, so they travel through the dedicated ``notify_host_status``
    dispatch path (task 05).
    """
    host: str
    event: str              # "down" | "recovered"
    # Populated only for "down" events (by convention, not the type).
    error: str | None = None

    @property
    def summary(self) -> str:
        """Human-readable one-line summary, shared by every notification channel."""
        if self.event == "recovered":
            return f"Host recovered: {self.host}"
        return f"Host down: {self.host} — {self.error or 'unreachable'}"


@dataclass
class ScanWarning:
    container_name: str
    image: str
    level: str              # "warning" | "error"
    message: str
    host: str = "local"     # which host this warning belongs to (see DOCKER_HOSTS)
