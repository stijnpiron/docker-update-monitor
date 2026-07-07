"""Fetch manifest lists (fat manifests) to verify multi-architecture support.

Queries the registry v2 manifest endpoint for a given image:tag and returns
the list of supported platforms. Single-arch images (no manifest list) are
treated as compatible. Results are cached per (image_name, tag) pair.
"""

import base64
from typing import Optional
from urllib.parse import urlparse

import requests

import app.http as _http
from app.config import log
from app.registry.base import detect_registry

# Module-level cache: (image_name, tag) → list[dict] | None
# list[dict]: platform entries from a multi-arch manifest list
# None: single-arch image; treat as compatible
_cache: dict[tuple[str, str], Optional[list[dict]]] = {}


def clear_cache() -> None:
    """Clear the manifest list cache. Used in tests."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_token(realm: str, service: str, scope: str, bearer_token: str = "",
               username: str = "", password: str = "") -> Optional[str]:
    """Obtain a Bearer token from a registry token endpoint."""
    headers: dict[str, str] = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    elif username and password:
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"

    params = {"service": service, "scope": scope}
    try:
        resp = _http.http_session.get(realm, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("token") or data.get("access_token")
    except Exception as exc:
        log.warning(f"Registry token request failed ({realm}): {exc}")
        return None


def _fetch_platforms_from_url(url: str, auth_headers: dict) -> Optional[list[dict]]:
    """
    GET the manifest URL and return the platform list if it is a manifest list,
    or None if it is a single-arch manifest.
    """
    headers = {
        **auth_headers,
        "Accept": (
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.oci.image.index.v1+json"
        ),
    }
    try:
        resp = _http.http_session.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        media_type = data.get("mediaType", "") or ""
        # Manifest list / OCI image index → has a manifests[] with platform info
        if "manifest.list" in media_type or "image.index" in media_type:
            return [m.get("platform", {}) for m in data.get("manifests", [])]
        # Fallback: schema v2 with a manifests[] key (e.g. some OCI indexes omit mediaType)
        if data.get("schemaVersion") == 2 and isinstance(data.get("manifests"), list):
            platforms = [m.get("platform", {}) for m in data["manifests"]]
            if platforms:
                return platforms
        # Single-arch manifest
        return None
    except requests.HTTPError as exc:
        log.warning(f"Manifest fetch HTTP error ({url}): {exc}")
        return None
    except Exception as exc:
        log.warning(f"Manifest fetch error ({url}): {exc}")
        return None


# ---------------------------------------------------------------------------
# Registry-specific fetchers
# ---------------------------------------------------------------------------

def _fetch_dockerhub_manifest_list(
    image_name: str, tag: str, username: str, password: str
) -> Optional[list[dict]]:
    name = image_name.removeprefix("docker.io/")
    parts = name.split("/")
    if len(parts) == 1:
        name = f"library/{name}"

    scope = f"repository:{name}:pull"
    token = _get_token(
        "https://auth.docker.io/token",
        service="registry.docker.io",
        scope=scope,
        username=username,
        password=password,
    )
    if not token:
        log.warning(f"DockerHub: could not obtain registry token for {name} — skipping arch check")
        return None

    url = f"https://registry-1.docker.io/v2/{name}/manifests/{tag}"
    return _fetch_platforms_from_url(url, {"Authorization": f"Bearer {token}"})


def _fetch_ghcr_manifest_list(
    image_name: str, tag: str, github_token: str
) -> Optional[list[dict]]:
    if not github_token:
        log.warning(f"GHCR: no GITHUB_TOKEN — skipping arch check for {image_name}")
        return None

    image_ref = image_name.strip()
    parsed = urlparse(image_ref)

    parsed_host = ""
    if parsed.scheme and parsed.netloc:
        parsed_host = (parsed.hostname or "").lower()
    elif "/" in image_ref:
        parsed_host = image_ref.split("/", 1)[0].lower()

    host = "lscr.io" if parsed_host == "lscr.io" else "ghcr.io"
    path = image_ref.removeprefix(f"{host}/")

    # Exchange GitHub PAT for a registry-scoped token
    scope = f"repository:{path}:pull"
    reg_token = _get_token(
        f"https://{host}/token",
        service=host,
        scope=scope,
        bearer_token=github_token,
    )
    if not reg_token:
        log.warning(f"GHCR: could not obtain registry token for {path} — skipping arch check")
        return None

    url = f"https://{host}/v2/{path}/manifests/{tag}"
    return _fetch_platforms_from_url(url, {"Authorization": f"Bearer {reg_token}"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_manifest_list(
    image_name: str,
    tag: str,
    dockerhub_username: str,
    dockerhub_password: str,
    github_token: str,
) -> Optional[list[dict]]:
    """Return the list of supported platforms for *image_name*:*tag*.

    Returns:
        list[dict]: platform dicts (keys: ``os``, ``architecture``, optionally ``variant``)
                    when the image has a manifest list.
        None: the image is single-arch or the manifest could not be fetched;
              the caller should treat this as compatible (current behaviour).

    Results are cached per (image_name, tag) for the lifetime of the process.
    """
    cache_key = (image_name, tag)
    if cache_key in _cache:
        return _cache[cache_key]

    registry = detect_registry(image_name)
    if registry == "dockerhub":
        result = _fetch_dockerhub_manifest_list(image_name, tag, dockerhub_username, dockerhub_password)
    elif registry == "ghcr":
        result = _fetch_ghcr_manifest_list(image_name, tag, github_token)
    else:
        log.debug(f"Unknown registry for '{image_name}' — skipping arch check")
        result = None

    _cache[cache_key] = result
    return result


def is_platform_supported(
    platforms: Optional[list[dict]],
    os: str,
    architecture: str,
) -> bool:
    """Return True if *os*/*architecture* is present in *platforms*.

    If *platforms* is ``None`` (single-arch image or manifest unavailable),
    returns ``True`` to preserve the existing (compatible) behaviour.
    """
    if platforms is None:
        return True
    return any(
        p.get("os") == os and p.get("architecture") == architecture
        for p in platforms
    )


# ---------------------------------------------------------------------------
# Digest fetching
# ---------------------------------------------------------------------------

# Cache: (image_name, tag) → digest string or None
_digest_cache: dict[tuple[str, str], Optional[str]] = {}


def clear_digest_cache() -> None:
    """Clear the digest cache. Used in tests."""
    _digest_cache.clear()


def _fetch_digest_from_url(url: str, auth_headers: dict) -> Optional[str]:
    """HEAD the manifest URL and return the Docker-Content-Digest header."""
    headers = {
        **auth_headers,
        "Accept": (
            "application/vnd.docker.distribution.manifest.v2+json,"
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.oci.image.index.v1+json"
        ),
    }
    try:
        resp = _http.http_session.head(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.headers.get("Docker-Content-Digest") or None
    except Exception as exc:
        log.warning(f"Digest fetch error ({url}): {exc}")
        return None


def _fetch_dockerhub_digest(image_name: str, tag: str, username: str, password: str) -> Optional[str]:
    name = image_name.removeprefix("docker.io/")
    parts = name.split("/")
    if len(parts) == 1:
        name = f"library/{name}"

    scope = f"repository:{name}:pull"
    token = _get_token(
        "https://auth.docker.io/token",
        service="registry.docker.io",
        scope=scope,
        username=username,
        password=password,
    )
    if not token:
        log.warning(f"DockerHub: could not obtain registry token for {name} — skipping digest fetch")
        return None

    url = f"https://registry-1.docker.io/v2/{name}/manifests/{tag}"
    return _fetch_digest_from_url(url, {"Authorization": f"Bearer {token}"})


def _fetch_ghcr_digest(image_name: str, tag: str, github_token: str) -> Optional[str]:
    if not github_token:
        log.warning(f"GHCR: no GITHUB_TOKEN — skipping digest fetch for {image_name}")
        return None

    image_ref = image_name.strip()
    parsed = urlparse(image_ref)

    parsed_host = ""
    if parsed.scheme and parsed.netloc:
        parsed_host = (parsed.hostname or "").lower()
    elif "/" in image_ref:
        parsed_host = image_ref.split("/", 1)[0].lower()

    host = "lscr.io" if parsed_host == "lscr.io" else "ghcr.io"
    path = image_ref.removeprefix(f"{host}/")

    scope = f"repository:{path}:pull"
    reg_token = _get_token(
        f"https://{host}/token",
        service=host,
        scope=scope,
        bearer_token=github_token,
    )
    if not reg_token:
        log.warning(f"GHCR: could not obtain registry token for {path} — skipping digest fetch")
        return None

    url = f"https://{host}/v2/{path}/manifests/{tag}"
    return _fetch_digest_from_url(url, {"Authorization": f"Bearer {reg_token}"})


def fetch_digest(
    image_name: str,
    tag: str,
    dockerhub_username: str,
    dockerhub_password: str,
    github_token: str,
) -> Optional[str]:
    """Return the manifest digest for *image_name*:*tag*.

    Returns:
        str: the Docker-Content-Digest value (e.g. "sha256:abc123...")
        None: could not fetch.

    Results are cached per (image_name, tag) for the lifetime of the process.
    """
    cache_key = (image_name, tag)
    if cache_key in _digest_cache:
        return _digest_cache[cache_key]

    registry = detect_registry(image_name)
    if registry == "dockerhub":
        result = _fetch_dockerhub_digest(image_name, tag, dockerhub_username, dockerhub_password)
    elif registry == "ghcr":
        result = _fetch_ghcr_digest(image_name, tag, github_token)
    else:
        log.debug(f"Unknown registry for '{image_name}' — skipping digest fetch")
        result = None

    _digest_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Platform-specific digest fetching (for multi-arch manifest lists)
# ---------------------------------------------------------------------------

# Cache: (image_name, tag, os, architecture) → digest string or None
_platform_digest_cache: dict[tuple[str, str, str, str], Optional[str]] = {}


def clear_platform_digest_cache() -> None:
    """Clear the platform digest cache. Used in tests."""
    _platform_digest_cache.clear()


def _fetch_platform_digest_from_url(
    url: str, auth_headers: dict, os: str, architecture: str
) -> Optional[str]:
    """GET the manifest URL and return the digest for the specified platform."""
    headers = {
        **auth_headers,
        "Accept": (
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.oci.image.index.v1+json"
        ),
    }
    try:
        resp = _http.http_session.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        media_type = data.get("mediaType", "") or ""
        manifests = None
        if "manifest.list" in media_type or "image.index" in media_type:
            manifests = data.get("manifests", [])
        elif data.get("schemaVersion") == 2 and isinstance(data.get("manifests"), list):
            manifests = data["manifests"]
        if not manifests:
            return None
        for m in manifests:
            platform = m.get("platform", {})
            if platform.get("os") == os and platform.get("architecture") == architecture:
                return m.get("digest") or None
        return None
    except requests.HTTPError as exc:
        log.warning(f"Platform digest fetch HTTP error ({url}): {exc}")
        return None
    except Exception as exc:
        log.warning(f"Platform digest fetch error ({url}): {exc}")
        return None


def _fetch_dockerhub_platform_digest(
    image_name: str, tag: str, os: str, architecture: str,
    username: str, password: str,
) -> Optional[str]:
    name = image_name.removeprefix("docker.io/")
    parts = name.split("/")
    if len(parts) == 1:
        name = f"library/{name}"

    scope = f"repository:{name}:pull"
    token = _get_token(
        "https://auth.docker.io/token",
        service="registry.docker.io",
        scope=scope,
        username=username,
        password=password,
    )
    if not token:
        log.warning(f"DockerHub: could not obtain registry token for {name} — skipping platform digest fetch")
        return None

    url = f"https://registry-1.docker.io/v2/{name}/manifests/{tag}"
    return _fetch_platform_digest_from_url(url, {"Authorization": f"Bearer {token}"}, os, architecture)


def _fetch_ghcr_platform_digest(
    image_name: str, tag: str, os: str, architecture: str, github_token: str,
) -> Optional[str]:
    if not github_token:
        log.warning(f"GHCR: no GITHUB_TOKEN — skipping platform digest fetch for {image_name}")
        return None

    image_ref = image_name.strip()
    parsed = urlparse(image_ref)

    parsed_host = ""
    if parsed.scheme and parsed.netloc:
        parsed_host = (parsed.hostname or "").lower()
    elif "/" in image_ref:
        parsed_host = image_ref.split("/", 1)[0].lower()

    host = "lscr.io" if parsed_host == "lscr.io" else "ghcr.io"
    path = image_ref.removeprefix(f"{host}/")

    scope = f"repository:{path}:pull"
    reg_token = _get_token(
        f"https://{host}/token",
        service=host,
        scope=scope,
        bearer_token=github_token,
    )
    if not reg_token:
        log.warning(f"GHCR: could not obtain registry token for {path} — skipping platform digest fetch")
        return None

    url = f"https://{host}/v2/{path}/manifests/{tag}"
    return _fetch_platform_digest_from_url(url, {"Authorization": f"Bearer {reg_token}"}, os, architecture)


def fetch_platform_digest(
    image_name: str,
    tag: str,
    os: str,
    architecture: str,
    dockerhub_username: str,
    dockerhub_password: str,
    github_token: str,
) -> Optional[str]:
    """Return the digest of the platform-specific manifest entry for *os*/*architecture*.

    Returns:
        str: the digest of the matching platform manifest (e.g. "sha256:abc123...")
        None: image is single-arch, platform not found in manifest list, or fetch failed.

    Results are cached per (image_name, tag, os, architecture).
    """
    cache_key = (image_name, tag, os, architecture)
    if cache_key in _platform_digest_cache:
        return _platform_digest_cache[cache_key]

    registry = detect_registry(image_name)
    if registry == "dockerhub":
        result = _fetch_dockerhub_platform_digest(
            image_name, tag, os, architecture, dockerhub_username, dockerhub_password
        )
    elif registry == "ghcr":
        result = _fetch_ghcr_platform_digest(image_name, tag, os, architecture, github_token)
    else:
        log.debug(f"Unknown registry for '{image_name}' — skipping platform digest fetch")
        result = None

    _platform_digest_cache[cache_key] = result
    return result
