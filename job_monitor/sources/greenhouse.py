from typing import List
from urllib.parse import parse_qs, urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import hash_job_id, normalize_text, parse_date


def extract_company_slug(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        return parts[0]
    raise ValueError(f"Unable to extract Greenhouse company from {url}")


def fetch_greenhouse_jobs(source: dict, session) -> List[Job]:
    company = source.get("company") or extract_company_slug(source["url"])
    query = parse_qs(urlparse(source["url"]).query)
    department_ids = {
        int(value)
        for value in (source.get("greenhouse_department_ids") or query.get("departments[]") or query.get("departments") or [])
        if str(value).isdigit()
    }
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"

    resp = request_with_retries(session, "GET", api_url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    jobs: List[Job] = []
    for item in data.get("jobs", []):
        if department_ids:
            item_departments = {
                int(dept.get("id"))
                for dept in item.get("departments", [])
                if isinstance(dept, dict) and str(dept.get("id", "")).isdigit()
            }
            if not item_departments.intersection(department_ids):
                continue

        title = normalize_text(item.get("title"))
        url = item.get("absolute_url")
        location = normalize_text(item.get("location", {}).get("name"))
        posted_at = parse_date(item.get("updated_at") or item.get("created_at"))

        if not title or not url:
            continue

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

    return jobs
