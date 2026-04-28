def detect_registry(image_name: str) -> str:
    """Return 'ghcr' | 'dockerhub' | 'unknown' based on the image name prefix."""
    if image_name.startswith("ghcr.io/"):
        return "ghcr"
    # docker.io prefix is sometimes explicit but usually omitted
    if "/" not in image_name or image_name.startswith("docker.io/"):
        return "dockerhub"
    # Two-part names like "linuxserver/sonarr" are DockerHub namespaced images
    parts = image_name.split("/")
    if len(parts) == 2 and "." not in parts[0]:
        return "dockerhub"
    return "unknown"
