def detect_registry(image_name: str) -> str:
    """Return 'ghcr' | 'dockerhub' | 'unknown' based on the image name prefix."""
    first_segment = image_name.split("/", 1)[0].lower()
    registry_host = first_segment.split(":", 1)[0]
    if registry_host == "ghcr.io":
        return "ghcr"
    # docker.io prefix is sometimes explicit but usually omitted
    if "/" not in image_name or image_name.startswith("docker.io/"):
        return "dockerhub"
    # Two-part names like "linuxserver/sonarr" are DockerHub namespaced images
    parts = image_name.split("/")
    if len(parts) == 2 and "." not in parts[0]:
        return "dockerhub"
    return "unknown"
