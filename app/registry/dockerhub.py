from typing import Optional

import requests

from app.config import log
import app.http as _http


def get_dockerhub_token(username: str, password: str) -> Optional[str]:
    """
    Exchange DockerHub credentials for a JWT token.
    Both plain passwords and Personal Access Tokens (PATs) are accepted as `password`.
    PATs are recommended — generate one at:
    https://app.docker.com/settings/personal-access-tokens
    """
    if not username or not password:
        log.info("DockerHub: no credentials — using anonymous access (rate-limited).")
        return None
    try:
        resp = _http.http_session.post(
            "https://hub.docker.com/v2/users/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json().get("token")
        log.info("DockerHub authentication successful.")
        return token
    except Exception as exc:
        log.warning(f"DockerHub auth failed: {exc}. Falling back to anonymous access.")
        return None


def _fetch_dockerhub_tags(image_name: str, token: Optional[str], current_tag: Optional[str] = None) -> list[str]:
    """Fetch tags from Docker Hub using the Hub v2 API.

    Docker Hub returns tags ordered by last_updated descending (newest first).
    When *current_tag* is given we stop paginating once the current tag appears
    on a page, because all subsequent pages contain only older entries.
    """
    # Strip explicit docker.io/ prefix if present
    name = image_name.removeprefix("docker.io/")
    parts = name.split("/")
    namespace = "library" if len(parts) == 1 else parts[0]
    repo = parts[-1]

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    tags: list[str] = []
    url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repo}/tags?page_size=100&ordering=last_updated"

    while url:
        try:
            resp = _http.http_session.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            page_tags = [t["name"] for t in data.get("results", [])]
            tags.extend(page_tags)

            # Early stop: current tag found on this page → older pages not needed
            if current_tag and current_tag in page_tags:
                log.debug(f"DockerHub: found current tag '{current_tag}' — stopping pagination")
                break

            url = data.get("next")
        except requests.HTTPError as exc:
            log.error(f"DockerHub HTTP error for {namespace}/{repo}: {exc}")
            break
        except Exception as exc:
            log.error(f"DockerHub error for {namespace}/{repo}: {exc}")
            break

    return tags
