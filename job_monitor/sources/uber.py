from typing import Dict, List
from urllib.parse import parse_qs, urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import hash_job_id, normalize_text, parse_date


SEARCH_API_URL = "https://www.uber.com/api/loadSearchJobsResults"
KNOWN_FILTER_KEYS = ("department", "lineOfBusinessName", "location", "programAndPlatform", "team")


def _extract_locale_code(source_url: str) -> str:
    parts = [part for part in urlparse(source_url).path.split("/") if part]
    if len(parts) >= 2 and len(parts[1]) == 2:
        return parts[1]
    return "en"


def _extract_search_filters(source_url: str) -> Dict[str, List[str]]:
    query = parse_qs(urlparse(source_url).query)
    filters: Dict[str, List[str]] = {}
    for key in KNOWN_FILTER_KEYS:
        values = [normalize_text(v) for v in query.get(key, [])]
        values = [v for v in values if v]
        filters[key] = values
    return filters


def _format_location_entry(entry: dict) -> str:
    city = normalize_text(entry.get("city"))
    region = normalize_text(entry.get("region"))
    country = normalize_text(entry.get("countryName") or entry.get("country"))
    parts = [part for part in (city, region, country) if part]
    return normalize_text(", ".join(parts))


def _country_token(value: str) -> str:
    normalized = normalize_text(value).lower()
    return normalized.replace(".", "")


def _is_us_country(value: str) -> bool:
    return _country_token(value) in {"usa", "us", "united states", "united states of america"}


def _is_us_job(item: dict) -> bool:
    locations: List[dict] = []
    primary = item.get("location")
    if isinstance(primary, dict):
        locations.append(primary)
    for loc in item.get("allLocations") or []:
        if isinstance(loc, dict):
            locations.append(loc)

    if not locations:
        return False

    for loc in locations:
        if _is_us_country(loc.get("countryName", "")) or _is_us_country(loc.get("country", "")):
            return True
    return False


def _format_location(item: dict) -> str:
    locations: List[str] = []

    primary = item.get("location")
    if isinstance(primary, dict):
        text = _format_location_entry(primary)
        if text:
            locations.append(text)

    for loc in item.get("allLocations") or []:
        if not isinstance(loc, dict):
            continue
        text = _format_location_entry(loc)
        if text:
            locations.append(text)

    deduped: List[str] = []
    seen = set()
    for value in locations:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return normalize_text("; ".join(deduped))


def fetch_uber_jobs(source: dict, session) -> List[Job]:
    page_size = int(source.get("uber_page_size", 100))
    max_pages = int(source.get("max_pages", 30))
    locale_code = source.get("uber_locale_code") or _extract_locale_code(source["url"])
    enforce_us_only = bool(source.get("uber_us_only", True))

    base_filters = _extract_search_filters(source["url"])

    jobs: List[Job] = []
    seen_job_ids = set()
    total = None

    for page in range(max_pages):
        payload = {
            "limit": page_size,
            "page": page,
            "params": base_filters,
        }
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.uber.com",
            "referer": source["url"],
            "x-csrf-token": "x",
        }
        resp = request_with_retries(
            session,
            "POST",
            SEARCH_API_URL,
            params={"localeCode": locale_code},
            json=payload,
            headers=headers,
            timeout=30,
            retries=3,
        )
        data = resp.json() or {}
        results = (((data.get("data") or {}).get("results")) or [])
        if not isinstance(results, list) or not results:
            break

        for item in results:
            if not isinstance(item, dict):
                continue
            if enforce_us_only and not _is_us_job(item):
                continue

            title = normalize_text(item.get("title"))
            posting_id = item.get("id")
            if not title or not posting_id:
                continue

            url = normalize_text(item.get("url") or item.get("jobUrl"))
            if not url:
                url = f"https://www.uber.com/global/en/careers/list/{posting_id}/"

            job_id = hash_job_id(source["name"], url)
            if job_id in seen_job_ids:
                continue
            seen_job_ids.add(job_id)

            posted_at = parse_date(item.get("updatedDate") or item.get("creationDate"))
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

        if total is None:
            total_block = ((data.get("data") or {}).get("totalResults"))
            if isinstance(total_block, int):
                total = total_block
            elif isinstance(total_block, dict):
                total = total_block.get("low")

        if len(results) < page_size:
            break
        if isinstance(total, int) and len(jobs) >= total:
            break

    return jobs
