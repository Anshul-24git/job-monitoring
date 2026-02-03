import json
from typing import Any, Iterable, List
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import Job
from ..http_utils import request_with_retries
from ..utils import ensure_absolute_url, extract_jsonld_objects, hash_job_id, normalize_text, parse_date

TITLE_KEYS = ("title", "jobTitle", "name", "positionTitle")
URL_KEYS = (
    "url",
    "jobUrl",
    "applyUrl",
    "externalUrl",
    "jobPostingUrl",
    "postingUrl",
    "detailUrl",
    "jobDetailUrl",
)
LOCATION_KEYS = ("location", "locationsText", "primaryLocation", "jobLocation", "city")
DATE_KEYS = ("datePosted", "postedAt", "postingDate", "updatedAt", "createdAt", "publishedAt")


def _extract_country(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name", "") or value.get("@id", "") or ""
    return ""


def extract_location_from_jsonld(obj: dict) -> str:
    loc = obj.get("jobLocation")
    if isinstance(loc, list):
        parts = []
        for entry in loc:
            address = entry.get("address", {}) if isinstance(entry, dict) else {}
            parts.append(
                normalize_text(
                    ", ".join(
                        [
                            address.get("addressLocality", ""),
                            address.get("addressRegion", ""),
                            _extract_country(address.get("addressCountry")),
                        ]
                    )
                )
            )
        return normalize_text("; ".join([p for p in parts if p]))
    if isinstance(loc, dict):
        address = loc.get("address", {})
        return normalize_text(
            ", ".join(
                [
                    address.get("addressLocality", ""),
                    address.get("addressRegion", ""),
                    _extract_country(address.get("addressCountry")),
                ]
            )
        )
    return ""


def iter_candidate_jobs(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        title = next((node.get(k) for k in TITLE_KEYS if node.get(k)), None)
        url = next((node.get(k) for k in URL_KEYS if node.get(k)), None)
        if title and (url or node.get("jobId") or node.get("jobPostingId")):
            yield node
        for value in node.values():
            yield from iter_candidate_jobs(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_candidate_jobs(item)


def parse_candidate_job(source: dict, base_url: str, item: dict) -> Job:
    title = normalize_text(next((item.get(k) for k in TITLE_KEYS if item.get(k)), ""))
    url = next((item.get(k) for k in URL_KEYS if item.get(k)), None)

    if not title:
        return None

    if not url:
        url = item.get("jobDetailUrl") or item.get("postingUrl")

    if not url:
        return None

    url = ensure_absolute_url(base_url, str(url))

    location = ""
    for key in LOCATION_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            location = normalize_text(value)
            break
    if not location and isinstance(item.get("jobLocation"), dict):
        location = extract_location_from_jsonld({"jobLocation": item.get("jobLocation")})

    posted_at = None
    for key in DATE_KEYS:
        value = item.get(key)
        if value:
            posted_at = parse_date(value)
            if posted_at:
                break

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


def fetch_generic_jobs(source: dict, session) -> List[Job]:
    url = source["url"]
    resp = request_with_retries(session, "GET", url, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    jobs: List[Job] = []

    selectors = source.get("selectors") or {}
    job_card_selector = selectors.get("job_card")

    if job_card_selector:
        for card in soup.select(job_card_selector):
            title_el = card.select_one(selectors.get("title", "")) if selectors.get("title") else None
            location_el = card.select_one(selectors.get("location", "")) if selectors.get("location") else None
            date_el = card.select_one(selectors.get("posted_at", "")) if selectors.get("posted_at") else None
            link_el = card.select_one(selectors.get("link", "a")) if selectors.get("link") else card.find("a")

            title = normalize_text(title_el.get_text()) if title_el else ""
            location = normalize_text(location_el.get_text()) if location_el else ""
            posted_at = parse_date(date_el.get_text()) if date_el else None
            link = link_el.get("href") if link_el else None

            if not title or not link:
                continue

            link = ensure_absolute_url(base_url, link)
            job_id = hash_job_id(source["name"], link)
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=link,
                    posted_at=posted_at,
                    raw={"selector": job_card_selector},
                )
            )

    if jobs:
        return jobs

    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            data = json.loads(next_data.string)
            for item in iter_candidate_jobs(data):
                job = parse_candidate_job(source, base_url, item)
                if job:
                    jobs.append(job)
        except Exception:
            pass

    if jobs:
        return jobs

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        for obj in extract_jsonld_objects(tag.string):
            title = normalize_text(obj.get("title"))
            link = obj.get("url")
            location = extract_location_from_jsonld(obj)
            posted_at = parse_date(obj.get("datePosted"))

            if not title or not link:
                continue

            link = ensure_absolute_url(base_url, link)
            job_id = hash_job_id(source["name"], link)
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=link,
                    posted_at=posted_at,
                    raw=obj,
                )
            )

    return jobs
