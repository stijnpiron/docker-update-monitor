from dataclasses import dataclass, field


@dataclass
class UpdateInfo:
    container_name: str
    service_name: str
    stack: str
    image: str
    current_version: str
    new_version: str
    update_type: str        # "patch" | "minor" | "major"
    status: str = ""        # "new" | "known" | "resolved"
    first_seen_at: str | None = None


@dataclass
class RegexMismatch:
    container_name: str
    service_name: str
    stack: str
    image: str
    current_tag: str
    pattern: str
    reason: str             # e.g. "did not match current tag"


@dataclass
class ScanWarning:
    container_name: str
    image: str
    level: str              # "warning" | "error"
    message: str
