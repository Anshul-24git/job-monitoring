from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import sync_playwright

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


OLD_SEARCH_API_URL = "https://www.uber.com/api/loadSearchJobsResults"
NEW_SEARCH_API_PATH = "/api/jobs/search/"
NEW_BASE_URL = "https://jobs.uber.com"
KNOWN_FILTER_KEYS = ("department", "lineOfBusinessName", "location", "programAndPlatform", "team")


def _extract_locale_code(source_url: str) -> str:
    parts = [part for part in urlparse(source_url).path.split("/") if part]
    if len(parts) >= 2 and len(parts[1]) == 2:
        return parts[1]
    if parts and len(parts[0]) == 2:
        return parts[0]
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
    city = normalize_text(entry.get("city") or entry.get("City"))
    region = normalize_text(entry.get("region") or entry.get("Region"))
    country = normalize_text(entry.get("countryName") or entry.get("country") or entry.get("Country"))
    parts = [part for part in (city, region, country) if part]
    return normalize_text(", ".join(parts))


def _country_token(value: str) -> str:
    normalized = normalize_text(value).lower()
    return normalized.replace(".", "")


def _is_us_country(value: str) -> bool:
    return _country_token(value) in {"usa", "us", "united states", "united states of america"}


def _old_is_us_job(item: dict) -> bool:
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


def _new_is_us_job(item: dict) -> bool:
    for loc in item.get("Locations") or []:
        if not isinstance(loc, dict):
            continue
        if _is_us_country(loc.get("Country", "")) or _is_us_country(loc.get("CountryCode", "")):
            return True
        address = normalize_text(loc.get("Address"))
        if "United States" in address or address.upper().endswith(" USA"):
            return True
    return False


def _old_format_location(item: dict) -> str:
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

    return _dedupe_join(locations)


def _new_format_location(item: dict) -> str:
    locations = []
    for loc in item.get("Locations") or []:
        if not isinstance(loc, dict):
            continue
        text = _format_location_entry(loc)
        if not text:
            text = normalize_text(loc.get("Address"))
        if text:
            locations.append(text)
    return _dedupe_join(locations)


def _dedupe_join(values: List[str]) -> str:
    deduped: List[str] = []
    seen = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return normalize_text("; ".join(deduped))


def _new_job_url(item: dict) -> str:
    for entry in item.get("Urls") or []:
        if not isinstance(entry, dict):
            continue
        value = normalize_text(entry.get("Url"))
        if value:
            return ensure_absolute_url(NEW_BASE_URL, value)
    posting_id = normalize_text(item.get("Id") or item.get("Reference"))
    return ensure_absolute_url(NEW_BASE_URL, f"/en/jobs/{posting_id}/")


def _build_new_search_params(source_url: str, *, page: int, locale_code: str) -> Dict[str, str]:
    query = parse_qs(urlparse(source_url).query)
    params: Dict[str, str] = {}
    for key, values in query.items():
        if not values:
            continue
        params[key] = normalize_text(values[0])
    params.setdefault("locale", locale_code)
    params["page"] = str(page)
    return params


def _fetch_new_uber_jobs(source: dict) -> List[Job]:
    locale_code = source.get("uber_locale_code") or _extract_locale_code(source["url"])
    enforce_us_only = bool(source.get("uber_us_only", True))
    max_pages = int(source.get("max_pages", 30))
    navigation_timeout_ms = int(source.get("uber_playwright_timeout_ms", 90000))

    jobs: List[Job] = []
    seen_job_ids = set()

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
                )
            )
            page.goto(source["url"], wait_until="networkidle", timeout=navigation_timeout_ms)

            total_pages: Optional[int] = None
            for page_number in range(1, max_pages + 1):
                params = _build_new_search_params(source["url"], page=page_number, locale_code=locale_code)
                payload = page.evaluate(
                    """async ({path, params}) => {
                        const qs = new URLSearchParams(params);
                        const response = await fetch(`${path}?${qs.toString()}`, {
                            headers: {accept: 'application/json'},
                        });
                        if (!response.ok) {
                            throw new Error(`Uber jobs API returned ${response.status}`);
                        }
                        return await response.json();
                    }""",
                    {"path": NEW_SEARCH_API_PATH, "params": params},
                )

                results = payload.get("jobs") or []
                if not isinstance(results, list) or not results:
                    break

                for item in results:
                    if not isinstance(item, dict):
                        continue
                    if enforce_us_only and not _new_is_us_job(item):
                        continue

                    title = normalize_text(item.get("Title"))
                    posting_id = normalize_text(item.get("Id") or item.get("Reference"))
                    if not title or not posting_id:
                        continue

                    url = _new_job_url(item)
                    job_id = hash_job_id(source["name"], url)
                    if job_id in seen_job_ids:
                        continue
                    seen_job_ids.add(job_id)

                    jobs.append(
                        Job(
                            job_id=job_id,
                            source=source["name"],
                            title=title,
                            location=_new_format_location(item),
                            url=url,
                            posted_at=parse_date(item.get("DisplayDate")),
                            raw=item,
                        )
                    )

                total_pages = payload.get("totalPages") if total_pages is None else total_pages
                if isinstance(total_pages, int) and page_number >= total_pages:
                    break
        finally:
            if browser is not None:
                browser.close()

    return jobs


def _fetch_old_uber_jobs(source: dict, session) -> List[Job]:
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
            OLD_SEARCH_API_URL,
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
            if enforce_us_only and not _old_is_us_job(item):
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
                    location=_old_format_location(item),
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


def fetch_uber_jobs(source: dict, session) -> List[Job]:
    host = urlparse(source["url"]).netloc.lower()
    if "jobs.uber.com" in host:
        return _fetch_new_uber_jobs(source)
    return _fetch_old_uber_jobs(source, session)
