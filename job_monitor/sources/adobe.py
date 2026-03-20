from typing import Dict, List
from urllib.parse import parse_qs, urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


ADOBE_WIDGETS_URL = "https://careers.adobe.com/widgets"
ADOBE_BASE_URL = "https://careers.adobe.com"


def _extract_subsearch(source_url: str, source: dict) -> str:
    if source.get("adobe_subsearch"):
        return normalize_text(source["adobe_subsearch"])
    query = parse_qs(urlparse(source_url).query)
    keyword = normalize_text((query.get("keywords") or query.get("keyword") or [""])[0])
    if keyword:
        return keyword
    return "Software"


def _is_us_job(item: dict) -> bool:
    country = normalize_text(item.get("country")).lower()
    if "united states" in country or country in {"us", "usa"}:
        return True

    location = normalize_text(item.get("location") or item.get("cityStateCountry")).lower()
    if "united states" in location:
        return True

    for text in item.get("multi_location") or []:
        if "united states" in normalize_text(text).lower():
            return True

    return False


def _format_location(item: dict) -> str:
    locations: List[str] = []
    for text in item.get("multi_location") or []:
        normalized = normalize_text(text)
        if normalized:
            locations.append(normalized)
    if not locations:
        primary = normalize_text(item.get("location") or item.get("cityStateCountry") or item.get("cityState"))
        if primary:
            locations.append(primary)

    deduped: List[str] = []
    seen = set()
    for value in locations:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return normalize_text("; ".join(deduped))


def _build_payload(offset: int, size: int, subsearch: str) -> Dict:
    return {
        "lang": "en_us",
        "deviceType": "desktop",
        "country": "us",
        "pageName": "Engineering and Product jobs",
        "ddoKey": "refineSearch",
        "sortBy": "Most recent",
        "subsearch": subsearch,
        "from": offset,
        "irs": False,
        "jobs": True,
        "counts": True,
        "all_fields": [
            "remote",
            "country",
            "state",
            "city",
            "experienceLevel",
            "category",
            "profession",
            "employmentType",
            "jobLevel",
        ],
        "pageType": "category",
        "size": size,
        "ak": "",
        "clearAll": False,
        "jdsource": "facets",
        "isSliderEnable": False,
        "pageId": "page62-ds",
        "siteType": "external",
        "keywords": "",
        "global": True,
        "selected_fields": {
            "category": ["Engineering and Product"],
            "country": ["United States of America"],
        },
        "sort": {
            "order": "desc",
            "field": "postedDate",
        },
        "locationData": {},
    }


def fetch_adobe_jobs(source: dict, session) -> List[Job]:
    page_size = int(source.get("adobe_page_size", 50))
    max_pages = int(source.get("max_pages", 20))
    enforce_us_only = bool(source.get("adobe_us_only", True))
    subsearch = _extract_subsearch(source["url"], source)

    jobs: List[Job] = []
    seen_job_ids = set()
    offset = 0

    for _ in range(max_pages):
        payload = _build_payload(offset, page_size, subsearch)
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": ADOBE_BASE_URL,
            "referer": source["url"],
        }
        csrf_token = source.get("adobe_csrf_token")
        if csrf_token:
            headers["x-csrf-token"] = csrf_token

        resp = request_with_retries(
            session,
            "POST",
            ADOBE_WIDGETS_URL,
            json=payload,
            headers=headers,
            timeout=30,
            retries=3,
        )
        payload_json = resp.json() or {}
        block = payload_json.get("refineSearch") if isinstance(payload_json, dict) else {}
        if not isinstance(block, dict):
            break

        data = block.get("data") or {}
        rows = data.get("jobs") or []
        if not isinstance(rows, list) or not rows:
            break

        for item in rows:
            if not isinstance(item, dict):
                continue
            if enforce_us_only and not _is_us_job(item):
                continue

            title = normalize_text(item.get("title"))
            url = normalize_text(item.get("applyUrl") or item.get("url"))
            if not title or not url:
                continue

            url = ensure_absolute_url(ADOBE_BASE_URL, url)
            job_id = hash_job_id(source["name"], url)
            if job_id in seen_job_ids:
                continue
            seen_job_ids.add(job_id)

            posted_at = parse_date(item.get("postedDate") or item.get("dateCreated"))
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=_format_location(item),
                    url=url,
                    posted_at=posted_at,
                    raw=item,
                )
            )

        hits = block.get("hits")
        total_hits = block.get("totalHits")
        consumed = hits if isinstance(hits, int) and hits > 0 else len(rows)
        offset += consumed

        if consumed < page_size:
            break
        if isinstance(total_hits, int) and offset >= total_hits:
            break

    return jobs
