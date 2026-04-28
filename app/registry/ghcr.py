import base64
from typing import Optional

import requests

from app.config import log
import app.http as _http


def _get_ghcr_token(owner: str, repo: str, github_token: str) -> Optional[str]:
    """Exchange a GitHub PAT for a short-lived GHCR pull token."""
    # GHCR uses the standard OCI token endpoint
    auth = base64.b64encode(f"token:{github_token}".encode()).decode()
    try:
        resp = _http.http_session.get(
            f"https://ghcr.io/token?service=ghcr.io&scope=repository:{owner}/{repo}:pull",
            headers={"Authorization": f"Basic {auth}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("token")
    except Exception as exc:
        log.warning(f"GHCR token exchange failed for {owner}/{repo}: {exc}")
        return None


def _fetch_ghcr_tags(image_name: str, github_token: str) -> list[str]:
    """
    Fetch all tags from GitHub Container Registry using the OCI v2 API.
    image_name should include the ghcr.io/ prefix, e.g. ghcr.io/owner/repo.
    Pagination follows the Link: <url>; rel="next" header.
    """
    # Strip ghcr.io/ → owner/repo (may be owner/repo or owner/group/repo)
    path = image_name.removeprefix("ghcr.io/")
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

    pull_token = _get_ghcr_token(owner, repo, github_token)
    if not pull_token:
        return []

    headers = {"Authorization": f"Bearer {pull_token}"}
    tags: list[str] = []
    url = f"https://ghcr.io/v2/{owner}/{repo}/tags/list?n=100"

    while url:
        try:
            resp = _http.http_session.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            tags.extend(data.get("tags") or [])

            # OCI pagination uses the Link header, not a next field in the body
            link = resp.headers.get("Link", "")
            next_url = None
            for part in link.split(","):
                part = part.strip()
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break
            url = next_url
        except requests.HTTPError as exc:
            log.error(f"GHCR HTTP error for {owner}/{repo}: {exc}")
            break
        except Exception as exc:
            log.error(f"GHCR error for {owner}/{repo}: {exc}")
            break

    return tags
