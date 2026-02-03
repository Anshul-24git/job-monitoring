from typing import Dict, List
from urllib.parse import parse_qs, urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


API_URL = "https://apply.careers.microsoft.com/api/pcsx/search"
BASE_URL = "https://apply.careers.microsoft.com"


def _build_params(source_url: str, start: int) -> Dict[str, str]:
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)

    params: Dict[str, str] = {
        "domain": "microsoft.com",
        "query": (query.get("query") or [""])[0],
        "location": (query.get("location") or [""])[0],
        "start": str(start),
        "sort_by": (query.get("sort_by") or ["timestamp"])[0],
        "filter_include_remote": (query.get("filter_include_remote") or ["1"])[0],
    }

    for key in (
        "filter_career_discipline",
        "filter_profession",
        "filter_job_family",
        "filter_role_type",
        "filter_employment_type",
    ):
        if key in query and query[key]:
            params[key] = query[key][0]

    return {k: v for k, v in params.items() if v}


def fetch_microsoft_jobs(source: dict, session) -> List[Job]:
    max_pages = int(source.get("max_pages", 5))
    jobs: List[Job] = []
    start = 0

    for _ in range(max_pages):
        params = _build_params(source["url"], start)
        resp = request_with_retries(session, "GET", API_URL, params=params, timeout=20)
        resp.raise_for_status()
        payload = resp.json() or {}
        data = payload.get("data") or {}
        positions = data.get("positions") or []

        if not positions:
            break

        for item in positions:
            title = normalize_text(item.get("name"))
            position_url = item.get("positionUrl")
            if not title or not position_url:
                continue

            url = ensure_absolute_url(BASE_URL, position_url)
            locations = item.get("standardizedLocations") or item.get("locations") or []
            location = normalize_text("; ".join(locations))
            posted_at = parse_date(item.get("postedTs") or item.get("creationTs"))

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

        start += len(positions)
        total = data.get("count")
        if isinstance(total, int) and start >= total:
            break

    return jobs
