from typing import Optional

from app.config import log
from app.registry.base import detect_registry
from app.registry.dockerhub import get_dockerhub_token, _fetch_dockerhub_tags
from app.registry.ghcr import _fetch_ghcr_tags


def fetch_all_tags(image_name: str, dockerhub_token: Optional[str], github_token: str,
                   current_tag: Optional[str] = None) -> list[str]:
    """Route to the correct registry fetcher based on the image name."""
    registry = detect_registry(image_name)

    if registry == "dockerhub":
        tags = _fetch_dockerhub_tags(image_name, dockerhub_token, current_tag)
        log.info(f"  Fetched {len(tags):4d} tags  ←  DockerHub  {image_name}")
    elif registry == "ghcr":
        tags = _fetch_ghcr_tags(image_name, github_token, current_tag)
        log.info(f"  Fetched {len(tags):4d} tags  ←  GHCR       {image_name}")
    else:
        log.warning(f"  Unsupported registry for '{image_name}' — skipping. "
                    "Only DockerHub and ghcr.io are supported.")
        tags = []

    return tags
