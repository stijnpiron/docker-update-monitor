import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def create_http_session() -> requests.Session:
    """Create a requests.Session with connection pooling and retry logic.

    Retries up to 3 times on 429/5xx with exponential backoff and
    respects the Retry-After header on HTTP 429 responses.
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


http_session = create_http_session()
