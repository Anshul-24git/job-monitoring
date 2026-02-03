from typing import List
from urllib.parse import urlparse

from ..models import Job
from ..utils import hash_job_id, normalize_text, parse_date


def extract_company_slug(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        return parts[0]
    raise ValueError(f"Unable to extract SmartRecruiters company from {url}")


def format_location(loc: dict) -> str:
    if not loc:
        return ""
    pieces = [loc.get("city"), loc.get("region"), loc.get("country")]
    return normalize_text(", ".join([p for p in pieces if p]))


def fetch_smartrecruiters_jobs(source: dict, session) -> List[Job]:
    company = source.get("company") or extract_company_slug(source["url"])
    api_url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"

    jobs: List[Job] = []
    offset = 0
    limit = 100

    while True:
        resp = session.get(api_url, params={"limit": limit, "offset": offset}, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        content = data.get("content", [])
        if not content:
            break

        for item in content:
            title = normalize_text(item.get("name"))
            ref = item.get("ref")
            url = f"https://careers.smartrecruiters.com/{company}/{ref}" if ref else None
            location = format_location(item.get("location", {}))
            posted_at = parse_date(item.get("releasedDate"))

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

        offset += limit
        total_found = data.get("totalFound")
        if total_found is not None and offset >= int(total_found):
            break

    return jobs

