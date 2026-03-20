from typing import List

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import hash_job_id, normalize_text, parse_date


LISTINGS_URL = "https://www.atlassian.com/endpoint/careers/listings"


def fetch_atlassian_jobs(source: dict, session) -> List[Job]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "X-Requested-With": "XMLHttpRequest",
    }
    resp = request_with_retries(session, "GET", LISTINGS_URL, timeout=30, retries=3, headers=headers)
    data = resp.json() or []

    jobs: List[Job] = []
    seen = set()
    for item in data:
        title = normalize_text(item.get("title"))
        portal = item.get("portalJobPost") or {}
        url = normalize_text(portal.get("portalUrl"))
        if not title or not url:
            continue

        job_id = hash_job_id(source["name"], url)
        if job_id in seen:
            continue
        seen.add(job_id)

        location = normalize_text(item.get("locations"))
        posted_at = parse_date(portal.get("updatedDate"))
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
