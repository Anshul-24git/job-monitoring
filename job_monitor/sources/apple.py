import json
import re
from typing import Dict, List
from urllib.parse import parse_qs, urlparse

import requests

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


API_URL = "https://jobs.apple.com/api/v1/search"
BASE_URL = "https://jobs.apple.com"
DETAIL_BASE = "https://jobs.apple.com/en-us/details"


def _location_filter_from_query(value: str) -> List[str]:
    if not value:
        return []
    value = value.strip()
    if not value:
        return []
    if "USA" in value.upper() or "UNITED-STATES" in value.lower():
        return ["postLocation-USA"]
    return []


def _build_payload(source_url: str, page: int) -> Dict:
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)

    search = (query.get("search") or query.get("query") or [""])[0]
    location = (query.get("location") or [""])[0]
    sort = (query.get("sort") or ["newest"])[0]

    payload: Dict = {
        "query": search,
        "filters": {},
        "page": page,
        "locale": "en-us",
        "sort": sort,
        "format": {
            "longDate": "MMMM D, YYYY",
            "mediumDate": "MMM D, YYYY",
        },
    }

    location_filters = _location_filter_from_query(location)
    if location_filters:
        payload["filters"]["locations"] = location_filters

    return payload


def _extract_csrf(html: str) -> str:
    match = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'name="csrf-token"\s*content="([^"]+)"', html)
    if match:
        return match.group(1)
    return ""


def _prepare_headers(source_url: str, csrf_token: str | None) -> Dict[str, str]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": source_url,
        "browserlocale": "en-us",
        "locale": "en_US",
    }
    if csrf_token:
        headers["x-apple-csrf-token"] = csrf_token
    return headers


def _fetch_page(session: requests.Session, source_url: str, payload: Dict) -> Dict:
    csrf_token = ""
    headers = _prepare_headers(source_url, csrf_token)
    try:
        resp = request_with_retries(session, "POST", API_URL, json=payload, headers=headers, timeout=20)
        return resp.json() or {}
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in {401, 403}:
            raise

    landing = request_with_retries(session, "GET", source_url, timeout=20)
    csrf_token = landing.headers.get("x-apple-csrf-token") or _extract_csrf(landing.text)
    headers = _prepare_headers(source_url, csrf_token)
    resp = request_with_retries(session, "POST", API_URL, json=payload, headers=headers, timeout=20)
    return resp.json() or {}


def _build_job_url(position_id: str, slug: str) -> str:
    if position_id and slug:
        return f"{DETAIL_BASE}/{position_id}/{slug}"
    if position_id:
        return f"{DETAIL_BASE}/{position_id}"
    return ""


def fetch_apple_jobs(source: dict, session) -> List[Job]:
    max_pages = int(source.get("max_pages", 5))
    jobs: List[Job] = []

    for page in range(1, max_pages + 1):
        payload = _build_payload(source["url"], page)
        data = _fetch_page(session, source["url"], payload)
        res = data.get("res") or {}
        items = res.get("searchResults") or []
        if not items:
            break

        for item in items:
            title = normalize_text(item.get("postingTitle"))
            position_id = item.get("positionId")
            slug = item.get("transformedPostingTitle")
            url = _build_job_url(position_id, slug)
            if not title or not url:
                continue

            locations = item.get("locations") or []
            location_parts = []
            for loc in locations:
                name = loc.get("name") or loc.get("metro") or loc.get("city") or ""
                country = loc.get("countryName") or ""
                pieces = [part for part in [name, country] if part]
                if pieces:
                    location_parts.append(", ".join(pieces))
            location = normalize_text("; ".join(location_parts))

            posted_at = parse_date(item.get("postDateInGMT"))
            job_id = hash_job_id(source["name"], url)

            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=url,
                    posted_at=posted_at,
                    raw=item,
                )
            )

        total = res.get("totalRecords")
        if isinstance(total, int) and page * len(items) >= total:
            break

    return jobs
