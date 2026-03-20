from datetime import datetime, timedelta, timezone
import re
from typing import List
from urllib.parse import parse_qs, urlencode, urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


TARGET_ROLE_PATTERNS = (
    re.compile(r"\bsoftware development engineer\b", re.I),
    re.compile(r"\bsoftware dev(?:elopment)? engineer\b", re.I),
    re.compile(r"\bsoftware engineer\b", re.I),
    re.compile(r"\bsde\b", re.I),
)
SENIOR_ROLE_PATTERN = re.compile(r"\b(senior|sr\.?|staff|principal)\b", re.I)


def _is_target_amazon_role(title: str) -> bool:
    normalized = normalize_text(title)
    if not normalized:
        return False
    if SENIOR_ROLE_PATTERN.search(normalized):
        return False
    return any(pattern.search(normalized) for pattern in TARGET_ROLE_PATTERNS)


def _is_us_job(item: dict) -> bool:
    country_code = normalize_text(item.get("country_code")).upper()
    if country_code in {"US", "USA"}:
        return True

    location = normalize_text(item.get("location")).upper()
    if location.startswith("US,") or " UNITED STATES" in location:
        return True

    locations = item.get("locations")
    if isinstance(locations, list):
        for value in locations:
            text = normalize_text(value).upper()
            if text.startswith("US,") or " UNITED STATES" in text:
                return True

    return False


def _build_search_json_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    path = parsed.path.rstrip("/")
    if path.endswith(".json"):
        json_path = path
    else:
        json_path = f"{path}.json"
    return f"{parsed.scheme}://{parsed.netloc}{json_path}"


def fetch_amazon_jobs(source: dict, session) -> List[Job]:
    source_url = source["url"]
    parsed = urlparse(source_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    search_json_url = _build_search_json_url(source_url)

    query = parse_qs(parsed.query)
    start_offset = int((query.get("offset") or ["0"])[0] or 0)
    page_size = int(source.get("amazon_result_limit") or (query.get("result_limit") or ["25"])[0] or 25)
    max_pages = int(source.get("max_pages", 25))
    max_age_days = int(source.get("amazon_max_posted_age_days", 7))

    # Keep the original query as-is and just update pagination values.
    query["offset"] = [str(start_offset)]
    query["result_limit"] = [str(page_size)]

    jobs: List[Job] = []
    seen = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days) if max_age_days > 0 else None
    offset = start_offset
    total_hits = None

    for _ in range(max_pages):
        query["offset"] = [str(offset)]
        params = urlencode(query, doseq=True)
        url = f"{search_json_url}?{params}"

        resp = request_with_retries(session, "GET", url, timeout=30, retries=3)
        payload = resp.json() or {}
        rows = payload.get("jobs") or []
        if not isinstance(rows, list) or not rows:
            break

        if total_hits is None:
            hits = payload.get("hits")
            if isinstance(hits, int):
                total_hits = hits

        for item in rows:
            if not isinstance(item, dict):
                continue

            if not _is_us_job(item):
                continue

            title = normalize_text(item.get("title"))
            if not _is_target_amazon_role(title):
                continue

            job_path = normalize_text(item.get("job_path"))
            if not job_path:
                id_icims = normalize_text(item.get("id_icims"))
                if not id_icims:
                    continue
                parts = [part for part in parsed.path.split("/") if part]
                locale = parts[0] if parts and len(parts[0]) == 2 else "en"
                job_path = f"/{locale}/jobs/{id_icims}"

            job_url = ensure_absolute_url(base_url, job_path)
            location = normalize_text(item.get("location"))
            posted_at = parse_date(item.get("posted_date"))
            if cutoff and posted_at and posted_at < cutoff:
                continue

            job_id = hash_job_id(source["name"], job_url)
            if job_id in seen:
                continue
            seen.add(job_id)

            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=job_url,
                    posted_at=posted_at,
                    raw=item,
                )
            )

        offset += page_size
        if len(rows) < page_size:
            break
        if isinstance(total_hits, int) and offset >= total_hits:
            break

    return jobs
