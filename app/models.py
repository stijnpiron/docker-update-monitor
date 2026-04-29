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
