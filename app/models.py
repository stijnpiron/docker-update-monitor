from dataclasses import dataclass


@dataclass
class UpdateInfo:
    container_name: str
    stack: str
    image: str
    current_version: str
    new_version: str
    update_type: str        # "patch" | "minor" | "major"
