from typing import List
from urllib.parse import urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import hash_job_id, normalize_text, parse_date


def extract_company_slug(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        return parts[0]
    raise ValueError(f"Unable to extract Lever company from {url}")


def fetch_lever_jobs(source: dict, session) -> List[Job]:
    company = source.get("company") or extract_company_slug(source["url"])
    api_url = f"https://api.lever.co/v0/postings/{company}?mode=json"

    resp = request_with_retries(session, "GET", api_url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    jobs: List[Job] = []
    for item in data:
        title = normalize_text(item.get("text"))
        url = item.get("hostedUrl")
        location = normalize_text(item.get("categories", {}).get("location"))
        posted_at = parse_date(item.get("createdAt"))

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
