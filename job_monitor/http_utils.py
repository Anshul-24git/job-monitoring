import time
from typing import Any

import requests


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # Avoid advertising Brotli unless the runtime has a decoder installed.
    # Some job boards return raw br-compressed bytes to requests otherwise.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff_seconds: float = 1.0,
    **kwargs: Any,
) -> requests.Response:
    for attempt in range(retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code in {429, 500, 502, 503, 504}:
                if attempt < retries:
                    time.sleep(backoff_seconds * (2 ** attempt))
                    continue
            resp.raise_for_status()
            return resp
        except requests.RequestException:
            if attempt >= retries:
                raise
            time.sleep(backoff_seconds * (2 ** attempt))
    raise RuntimeError("request_with_retries: exhausted retries")
