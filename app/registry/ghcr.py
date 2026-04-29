from typing import Optional

import requests

from app.config import log
import app.http as _http

_MAX_PAGES = 100  # Safety cap to prevent infinite loops (100 × 100 = 10k versions)


def _fetch_ghcr_tags(image_name: str, github_token: str, current_tag: Optional[str] = None) -> list[str]:
    """
    Fetch tags from GitHub Container Registry using the GitHub Packages REST API.
    Uses the versions endpoint which returns results sorted by creation date
    (newest first), allowing early stop once the current tag is found.
    """
    # Strip ghcr.io/ or lscr.io/ → owner/repo (may be owner/repo or owner/group/repo)
    path = image_name.removeprefix("ghcr.io/").removeprefix("lscr.io/")
    parts = path.split("/")
    if len(parts) < 2:
        log.warning(f"GHCR: cannot parse owner/repo from '{image_name}'")
        return []
    owner = parts[0]
    repo = "/".join(parts[1:])

    if not github_token:
        log.warning(f"GHCR: no GITHUB_TOKEN set — cannot fetch tags for {image_name}. "
                    "Set GITHUB_TOKEN with read:packages scope.")
        return []

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tags: list[str] = []
    page = 1
    # Try org endpoint first; fall back to user endpoint on 404
    base_url = (f"https://api.github.com/orgs/{owner}/packages/container/"
                f"{repo}/versions")

    while page <= _MAX_PAGES:
        url = f"{base_url}?per_page=100&page={page}"
        try:
            resp = _http.http_session.get(url, headers=headers, timeout=20)

            # If org endpoint returns 404 on first page, switch to user endpoint
            if resp.status_code == 404 and page == 1:
                base_url = (f"https://api.github.com/users/{owner}/packages/container/"
                            f"{repo}/versions")
                url = f"{base_url}?per_page=100&page={page}"
                resp = _http.http_session.get(url, headers=headers, timeout=20)

            resp.raise_for_status()
            versions = resp.json()

            if not versions:
                break

            found_current = False
            for version in versions:
                version_tags = (version.get("metadata", {})
                                .get("container", {})
                                .get("tags") or [])
                tags.extend(version_tags)
                if current_tag and current_tag in version_tags:
                    found_current = True

            if found_current:
                log.debug(f"GHCR: found current tag '{current_tag}' — stopping pagination")
                break

            page += 1
        except requests.HTTPError as exc:
            log.error(f"GHCR HTTP error for {owner}/{repo}: {exc}")
            break
        except Exception as exc:
            log.error(f"GHCR error for {owner}/{repo}: {exc}")
            break

    return tags
