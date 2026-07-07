import re
from datetime import datetime, timedelta, timezone
from typing import List

from bs4 import BeautifulSoup

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text


HN_USER_RE = re.compile(r"^https://news\.ycombinator\.com/user\?id=", re.I)
RELATIVE_TIME_RE = re.compile(
    r"^(?P<num>\d+)\s+(?P<unit>minute|minutes|hour|hours|day|days)\s+ago$",
    re.I,
)


def _parse_relative_time(value: str) -> datetime | None:
    match = RELATIVE_TIME_RE.match(normalize_text(value))
    if not match:
        return None

    amount = int(match.group("num"))
    unit = match.group("unit").lower()
    if unit.startswith("minute"):
        delta = timedelta(minutes=amount)
    elif unit.startswith("hour"):
        delta = timedelta(hours=amount)
    else:
        delta = timedelta(days=amount)
    return datetime.now(timezone.utc) - delta


def _external_links(post, base_url: str) -> list[str]:
    links: list[str] = []
    for anchor in post.find_all("a", href=True):
        href = normalize_text(anchor.get("href"))
        if not href or HN_USER_RE.match(href):
            continue
        links.append(ensure_absolute_url(base_url, href))
    return links


def _post_title(lines: list[str], links: list[str]) -> str:
    for line in lines:
        lowered = line.lower()
        if not line:
            continue
        if RELATIVE_TIME_RE.match(line):
            continue
        if line.startswith("http://") or line.startswith("https://"):
            continue
        if lowered in {"remote", "onsite", "hybrid"}:
            continue
        if " | " in line or "hiring" in lowered or "engineer" in lowered or "developer" in lowered:
            return line
    return lines[0] if lines else (links[0] if links else "HNHiring post")


def fetch_hnhiring_jobs(source: dict, session) -> List[Job]:
    timeout = int(source.get("timeout_seconds", 30))
    retries = int(source.get("request_retries", 2))
    backoff = float(source.get("request_backoff_seconds", 1.0))
    url = source["url"]

    resp = request_with_retries(
        session,
        "GET",
        url,
        timeout=timeout,
        retries=retries,
        backoff_seconds=backoff,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    base_url = f"{resp.url.split('/')[0]}//{resp.url.split('/')[2]}"
    jobs: List[Job] = []
    seen: set[str] = set()

    for post in soup.select("li.job"):
        lines = [
            normalize_text(line)
            for line in post.get_text("\n", strip=True).splitlines()
            if normalize_text(line)
        ]
        if not lines:
            continue

        links = _external_links(post, base_url)
        if not links:
            continue

        title = _post_title(lines, links)
        post_url = links[0]
        posted_at = None
        for line in lines[:4]:
            posted_at = _parse_relative_time(line)
            if posted_at:
                break

        job_id = hash_job_id(source["name"], f"{title}::{post_url}")
        if job_id in seen:
            continue
        seen.add(job_id)

        jobs.append(
            Job(
                job_id=job_id,
                source=source["name"],
                title=title,
                location="",
                url=post_url,
                posted_at=posted_at,
                raw={
                    "source": "hnhiring",
                    "links": links,
                    "text": "\n".join(lines[:20]),
                },
            )
        )

    return jobs
