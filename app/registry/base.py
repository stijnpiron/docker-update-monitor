from urllib.parse import urlparse

def detect_registry(image_name: str) -> str:
    """Return 'ghcr' | 'dockerhub' | 'unknown' based on the image name prefix."""
    # If this looks like a URL, parse and validate by hostname instead of string prefix.
    if "://" in image_name:
        parsed = urlparse(image_name)
        host = parsed.hostname
        if host in ("ghcr.io", "lscr.io"):
            return "ghcr"
        if host == "docker.io":
            return "dockerhub"
        return "unknown"

    first_segment = image_name.split("/", 1)[0].lower()
    registry_host = first_segment.split(":", 1)[0]
    if registry_host in ("ghcr.io", "lscr.io"):
        return "ghcr"
    if registry_host == "docker.io":
        return "dockerhub"
    # No explicit registry prefix — simple or namespaced DockerHub images
    if "/" not in image_name:
        return "dockerhub"
    # Two-part names like "linuxserver/sonarr" are DockerHub namespaced images
    parts = image_name.split("/")
    if len(parts) == 2 and "." not in parts[0]:
        return "dockerhub"
    return "unknown"
