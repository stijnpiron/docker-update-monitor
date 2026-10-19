import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Single shared session: connection pooling + retries (up to 3 on 429/5xx
# with exponential backoff, respecting the Retry-After header on 429).
http_session = requests.Session()
_adapter = HTTPAdapter(
    max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    ),
    pool_connections=10,
    pool_maxsize=10,
)
http_session.mount("https://", _adapter)
http_session.mount("http://", _adapter)
