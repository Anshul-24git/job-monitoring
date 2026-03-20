from typing import List
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text


def _build_page_url(source_url: str, *, page: int) -> str:
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    rebuilt_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, rebuilt_query, parsed.fragment))


def fetch_salesforce_jobs(source: dict, session) -> List[Job]:
    parsed = urlparse(source["url"])
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    max_pages = int(source.get("max_pages", 80))

    jobs: List[Job] = []
    seen = set()

    for page in range(1, max_pages + 1):
        page_url = _build_page_url(source["url"], page=page)
        resp = request_with_retries(session, "GET", page_url, timeout=30, retries=3)
        soup = BeautifulSoup(resp.text, "lxml")

        cards = soup.select("div.card.card-job")
        if not cards:
            break

        page_added = 0
        for card in cards:
            link = card.select_one("h3.card-title a[href]")
            actions = card.select_one(".card-job-actions")
            if not link:
                continue

            title = normalize_text(link.get_text())
            href = normalize_text(link.get("href"))
            if not title or not href:
                continue

            job_url = ensure_absolute_url(base_url, href)
            job_id = normalize_text(actions.get("data-id") if actions else "")
            if not job_id:
                job_id = hash_job_id(source["name"], job_url)
            else:
                job_id = f"{source['name']}::{job_id}"

            if job_id in seen:
                continue
            seen.add(job_id)

            location_items = [normalize_text(node.get_text()) for node in card.select("ul.locations li")]
            location = ", ".join([item for item in location_items if item])

            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=job_url,
                    posted_at=None,
                    raw={"page": page},
                )
            )
            page_added += 1

        if page_added == 0:
            break

        next_link = soup.select_one("a[rel='next']")
        if not next_link:
            break

    return jobs
