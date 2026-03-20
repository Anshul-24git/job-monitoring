from typing import Dict, List
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, is_date_only_value, normalize_text, parse_date


def _build_params(source_url: str, start: int) -> Dict[str, str]:
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)
    domain = normalize_text((query.get("domain") or [""])[0])
    if not domain and "morganstanley.eightfold.ai" in parsed.netloc:
        domain = "morganstanley.com"
    if not domain and "jobs.nvidia.com" in parsed.netloc:
        domain = "nvidia.com"

    params: Dict[str, str] = {
        "domain": domain,
        "query": normalize_text((query.get("query") or [""])[0]),
        "location": normalize_text((query.get("location") or [""])[0]),
        "start": str(start),
        "sort_by": normalize_text((query.get("sort_by") or ["relevance"])[0]) or "relevance",
        "filter_include_remote": normalize_text((query.get("filter_include_remote") or [""])[0]),
        "hl": normalize_text((query.get("hl") or [""])[0]),
    }
    return {key: value for key, value in params.items() if value}


def _build_job_url(source_url: str, path: str) -> str:
    parsed = urlparse(source_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    query = parse_qs(parsed.query)
    params = {}
    for key in ("domain", "source", "customredirect", "hl"):
        value = normalize_text((query.get(key) or [""])[0])
        if value:
            params[key] = value

    url = ensure_absolute_url(base_url, path)
    if not params:
        return url

    job_parsed = urlparse(url)
    existing = parse_qs(job_parsed.query)
    for key, value in params.items():
        existing.setdefault(key, [value])
    rebuilt = urlencode(existing, doseq=True)
    return urlunparse((job_parsed.scheme, job_parsed.netloc, job_parsed.path, job_parsed.params, rebuilt, job_parsed.fragment))


def _extract_posted_fields(item: dict) -> tuple[object, object]:
    for key in ("postedTs", "postedAt", "datePosted", "postingDate", "creationTs", "t_update", "t_create"):
        value = item.get(key)
        if value not in (None, ""):
            return key, value
    return None, None


def fetch_eightfold_jobs(source: dict, session) -> List[Job]:
    parsed = urlparse(source["url"])
    api_url = f"{parsed.scheme}://{parsed.netloc}/api/pcsx/search"
    max_pages = int(source.get("max_pages", 10))
    page_size = int(source.get("eightfold_page_size", 10))
    timeout = int(source.get("timeout_seconds", 30))

    jobs: List[Job] = []
    seen = set()
    start = 0

    for _ in range(max_pages):
        params = _build_params(source["url"], start)
        resp = request_with_retries(session, "GET", api_url, params=params, timeout=timeout, retries=3)
        payload = resp.json() or {}
        data = payload.get("data") or {}
        positions = data.get("positions") or []
        if not positions:
            break

        for item in positions:
            title = normalize_text(item.get("name") or item.get("posting_name"))
            position_url = normalize_text(item.get("positionUrl") or item.get("canonicalPositionUrl"))
            if not title or not position_url:
                continue

            url = _build_job_url(source["url"], position_url)
            job_id = hash_job_id(source["name"], url)
            if job_id in seen:
                continue
            seen.add(job_id)

            location = normalize_text(item.get("locations") or item.get("standardizedLocations") or item.get("location"))
            posted_key, posted_raw = _extract_posted_fields(item)
            posted_at = parse_date(posted_raw)
            posted_is_date_only = is_date_only_value(posted_raw)
            raw = dict(item)
            if posted_key:
                raw["posted_at_key"] = posted_key
            if posted_raw not in (None, ""):
                raw["posted_at_text"] = normalize_text(posted_raw)
            raw["posted_at_is_date_only"] = posted_is_date_only

            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=url,
                    posted_at=posted_at,
                    raw=raw,
                )
            )

        start += len(positions)
        total = data.get("count")
        if isinstance(total, int) and start >= total:
            break
        if len(positions) < page_size:
            break

    return jobs
