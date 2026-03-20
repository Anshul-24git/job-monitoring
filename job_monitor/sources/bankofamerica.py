from typing import List
from urllib.parse import parse_qs, urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


def _build_location(item: dict) -> str:
    city = normalize_text(item.get("city"))
    state = normalize_text(item.get("state"))
    country = normalize_text(item.get("country"))
    pieces = [piece for piece in (city, state, country) if piece]
    return ", ".join(pieces)


def fetch_bankofamerica_jobs(source: dict, session) -> List[Job]:
    source_url = source["url"]
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    endpoint = ensure_absolute_url(base_url, "/services/jobssearchservlet")
    term = normalize_text((query.get("keywords") or query.get("term") or ["software engineer"])[0])
    rows = int(source.get("boa_rows", 50))
    max_pages = int(source.get("max_pages", 20))
    timeout = int(source.get("timeout_seconds", 45))
    retries = int(source.get("request_retries", 4))
    backoff_seconds = float(source.get("request_backoff_seconds", 1.5))

    params = {
        "term": term,
        "sort": normalize_text((query.get("sort") or ["newest"])[0]) or "newest",
        "search": normalize_text((query.get("search") or ["jobsByLocation"])[0]) or "jobsByLocation",
        "searchstring": normalize_text((query.get("searchstring") or ["United States"])[0]) or "United States",
        "rows": str(rows),
        "start": "0",
    }

    jobs: List[Job] = []
    seen = set()
    start = 0
    total_matches = None

    for _ in range(max_pages):
        params["start"] = str(start)
        resp = request_with_retries(
            session,
            "GET",
            endpoint,
            params=params,
            timeout=timeout,
            retries=retries,
            backoff_seconds=backoff_seconds,
        )
        payload = resp.json() or {}
        rows_data = payload.get("jobsList") or []
        if not isinstance(rows_data, list) or not rows_data:
            break

        if total_matches is None:
            try:
                total_matches = int(payload.get("totalMatches"))
            except Exception:
                total_matches = None

        for item in rows_data:
            if not isinstance(item, dict):
                continue

            title = normalize_text(item.get("postingTitle"))
            path = normalize_text(item.get("jcrURL"))
            if not title or not path:
                continue

            url = ensure_absolute_url(base_url, path)
            job_id = hash_job_id(source["name"], url)
            if job_id in seen:
                continue
            seen.add(job_id)

            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=_build_location(item),
                    url=url,
                    posted_at=parse_date(item.get("postedDate") or item.get("indexedDate")),
                    raw=item,
                )
            )

        start += len(rows_data)
        if len(rows_data) < rows:
            break
        if total_matches is not None and start >= total_matches:
            break

    return jobs
