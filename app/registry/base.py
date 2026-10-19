def detect_registry(image_name: str) -> str:
    """Return 'ghcr' | 'dockerhub' | 'unknown' based on the image name prefix."""
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
    # but only if the first part has no dot AND no colon (port means it's a registry address)
    parts = image_name.split("/")
    if len(parts) == 2 and "." not in parts[0] and ":" not in parts[0]:
        return "dockerhub"
    return "unknown"
