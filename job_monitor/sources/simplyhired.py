from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


BASE_URL = "https://www.simplyhired.com"
DEFAULT_QUERIES = (
    "software engineer",
    "software development engineer",
    "forward deployed engineer",
    "ai engineer",
    "ai software engineer",
)
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "IA",
    "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO",
    "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI",
    "WV", "WY", "DC", "PR",
}
STATE_RE = re.compile(r"(?:,|\b)\s*([A-Z]{2})(?:\b|\s|,)")


def _source_queries(source: dict) -> List[str]:
    queries = source.get("simplyhired_queries") or DEFAULT_QUERIES
    deduped: List[str] = []
    seen = set()
    for query in queries:
        text = normalize_text(query).lower()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _build_search_url(query: str, *, location: str = "", cursor: Optional[str] = None) -> str:
    params = {"q": query, "l": location}
    if cursor:
        params["cursor"] = cursor
    return f"{BASE_URL}/search?{urlencode(params)}"


def _load_page_props(html: str) -> Dict:
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return {}
    try:
        payload = json.loads(script.string)
    except json.JSONDecodeError:
        return {}
    return (((payload.get("props") or {}).get("pageProps")) or {})


def _is_us_or_remote(item: dict) -> bool:
    location = normalize_text(item.get("location"))
    location_lower = location.lower()
    remote_attrs = [normalize_text(value).lower() for value in item.get("remoteAttributes") or []]

    if location_lower == "remote" or "remote" in remote_attrs:
        return True
    if "united states" in location_lower or location_lower.endswith(" usa"):
        return True

    for match in STATE_RE.finditer(location):
        if match.group(1).upper() in US_STATE_CODES:
            return True
    return False


def _job_url(item: dict) -> str:
    bot_url = normalize_text(item.get("botUrl"))
    if bot_url:
        return ensure_absolute_url(BASE_URL, bot_url)
    job_key = normalize_text(item.get("jobKey"))
    if job_key:
        return ensure_absolute_url(BASE_URL, f"/job/{job_key}")
    encoded_url = normalize_text(item.get("encodedUrl"))
    if encoded_url:
        return ensure_absolute_url(BASE_URL, encoded_url)
    return ""


def _format_location(item: dict) -> str:
    location = normalize_text(item.get("location"))
    remote_attrs = [normalize_text(value) for value in item.get("remoteAttributes") or []]
    if remote_attrs and "remote" not in location.lower():
        return normalize_text("; ".join([location, *remote_attrs])) if location else "; ".join(remote_attrs)
    return location


def _iter_jobs_from_page(source: dict, page_props: Dict) -> Iterable[Job]:
    for item in page_props.get("jobs") or []:
        if not isinstance(item, dict):
            continue
        if bool(source.get("simplyhired_us_only", True)) and not _is_us_or_remote(item):
            continue

        title = normalize_text(item.get("title"))
        url = _job_url(item)
        if not title or not url:
            continue

        yield Job(
            job_id=hash_job_id(source["name"], url),
            source=source["name"],
            title=title,
            location=_format_location(item),
            url=url,
            posted_at=parse_date(item.get("dateOnIndeed")),
            raw=item,
        )


def fetch_simplyhired_jobs(source: dict, session) -> List[Job]:
    max_pages = int(source.get("simplyhired_max_pages", 1))
    location = normalize_text(source.get("simplyhired_location", ""))
    timeout = int(source.get("simplyhired_timeout", 30))
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        ),
    }

    jobs: List[Job] = []
    seen_job_ids = set()

    for query in _source_queries(source):
        cursor: Optional[str] = None
        for page_number in range(1, max_pages + 1):
            url = _build_search_url(query, location=location, cursor=cursor)
            resp = request_with_retries(session, "GET", url, headers=headers, timeout=timeout, retries=3)
            page_props = _load_page_props(resp.text)
            if not page_props:
                break

            for job in _iter_jobs_from_page(source, page_props):
                if job.job_id in seen_job_ids:
                    continue
                seen_job_ids.add(job.job_id)
                jobs.append(job)

            cursors = page_props.get("pageCursors") or {}
            next_cursor = cursors.get(str(page_number + 1))
            if not next_cursor:
                break
            cursor = normalize_text(next_cursor)
            if not cursor:
                break

    return jobs
