import json
from typing import Any, Iterable, List
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import Job
from ..utils import ensure_absolute_url, extract_jsonld_objects, hash_job_id, normalize_text, parse_date


def iter_job_candidates(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        for key in ("jobs", "jobPostings", "jobPostingList"):
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
        for value in node.values():
            yield from iter_job_candidates(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_job_candidates(item)


def parse_job_item(source: dict, base_url: str, item: dict) -> Job:
    title = normalize_text(item.get("title") or item.get("jobTitle"))
    url = item.get("jobUrl") or item.get("url") or item.get("applyUrl")
    location = normalize_text(item.get("location") or item.get("locationName") or "")

    if not title or not url:
        return None

    url = ensure_absolute_url(base_url, url)
    posted_at = parse_date(item.get("postedAt") or item.get("createdAt") or item.get("datePosted"))

    job_id = hash_job_id(source["name"], url)
    return Job(
        job_id=job_id,
        source=source["name"],
        title=title,
        location=location,
        url=url,
        posted_at=posted_at,
        raw=item,
    )


def fetch_ashby_jobs(source: dict, session) -> List[Job]:
    url = source["url"]
    resp = session.get(url, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    jobs: List[Job] = []

    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            data = json.loads(script.string)
            for item in iter_job_candidates(data):
                job = parse_job_item(source, base_url, item)
                if job:
                    jobs.append(job)
        except Exception:
            pass

    if jobs:
        return jobs

    # Fallback to JSON-LD JobPosting data
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        for obj in extract_jsonld_objects(tag.string):
            title = normalize_text(obj.get("title"))
            url = obj.get("url") or obj.get("hiringOrganization", {}).get("url")
            location = normalize_text(obj.get("jobLocation", {}).get("address", {}).get("addressLocality", ""))
            posted_at = parse_date(obj.get("datePosted"))

            if not title or not url:
                continue

            url = ensure_absolute_url(base_url, url)
            job_id = hash_job_id(source["name"], url)
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=url,
                    posted_at=posted_at,
                    raw=obj,
                )
            )

    return jobs

