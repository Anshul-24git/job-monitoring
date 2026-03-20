import json
import logging
import time
from typing import Any, Iterable, List
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, extract_jsonld_objects, hash_job_id, normalize_text, parse_date


ASHBY_SEARCH_API_URL_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
logger = logging.getLogger(__name__)


def iter_job_candidates(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        for key in ("jobs", "jobPostings", "jobPostingList", "results", "openings"):
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


def _extract_board_slug(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return ""
    return parts[0]


def _format_location(value: Any) -> str:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        return normalize_text(
            value.get("name")
            or value.get("locationName")
            or value.get("title")
            or ""
        )
    if isinstance(value, list):
        parts = [_format_location(item) for item in value]
        return normalize_text("; ".join([part for part in parts if part]))
    return ""


def parse_job_item(source: dict, base_url: str, item: dict) -> Job:
    candidate = dict(item)
    for nested_key in ("jobPosting", "posting", "job"):
        nested = candidate.get(nested_key)
        if isinstance(nested, dict):
            candidate.update(nested)

    title = normalize_text(
        candidate.get("title")
        or candidate.get("jobTitle")
        or candidate.get("externalTitle")
        or candidate.get("name")
    )
    url = (
        candidate.get("jobUrl")
        or candidate.get("url")
        or candidate.get("applyUrl")
        or candidate.get("jobPostingUrl")
        or candidate.get("absoluteUrl")
        or candidate.get("jobPostingApplicationLink")
        or candidate.get("applicationUrl")
    )
    if not url:
        slug = source.get("ashby_slug") or _extract_board_slug(source["url"])
        path = candidate.get("jobPath") or candidate.get("path") or candidate.get("slug") or candidate.get("id")
        if path and slug:
            url = f"/{slug}/{str(path).lstrip('/')}"
    location = _format_location(
        candidate.get("location")
        or candidate.get("locationName")
        or candidate.get("secondaryLocations")
        or candidate.get("locations")
    )
    posted_at = parse_date(
        candidate.get("postedAt")
        or candidate.get("postedDate")
        or candidate.get("createdAt")
        or candidate.get("createdAtMs")
        or candidate.get("datePosted")
        or candidate.get("publishedAt")
        or candidate.get("publishedDate")
        or candidate.get("updatedAt")
    )

    if not title or not url:
        return None

    url = ensure_absolute_url(base_url, url)

    job_id = hash_job_id(source["name"], url)
    return Job(
        job_id=job_id,
        source=source["name"],
        title=title,
        location=location,
        url=url,
        posted_at=posted_at,
        raw=candidate,
    )


def _dedupe_jobs(jobs: List[Job]) -> List[Job]:
    by_id = {}
    for job in jobs:
        by_id[job.job_id] = job
    return list(by_id.values())


def _extract_jobs_from_payload(source: dict, base_url: str, payload: Any) -> List[Job]:
    jobs: List[Job] = []
    for item in iter_job_candidates(payload):
        job = parse_job_item(source, base_url, item)
        if job:
            jobs.append(job)
    return _dedupe_jobs(jobs)


def _fetch_ashby_api_jobs(source: dict, session, base_url: str) -> List[Job]:
    slug = source.get("ashby_slug") or _extract_board_slug(source["url"])
    if not slug:
        return []

    api_url = ASHBY_SEARCH_API_URL_TEMPLATE.format(slug=slug)
    params = {"includeCompensation": "true"}
    headers = {
        "accept": "application/json",
        "user-agent": session.headers.get("User-Agent", ""),
    }

    resp = None
    last_exc = None
    for attempt in range(4):
        try:
            # Some Ashby boards respond differently to session-level browser headers.
            resp = requests.get(api_url, params=params, headers=headers, timeout=30)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(1.0 * (2 ** attempt))
                continue
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= 3:
                raise
            time.sleep(1.0 * (2 ** attempt))

    if resp is None:
        if last_exc is not None:
            raise last_exc
        return []

    content_type = (resp.headers.get("content-type") or "").lower()
    data = None
    if "json" in content_type:
        try:
            data = resp.json() or {}
        except requests.exceptions.JSONDecodeError:
            data = None
    if data is None:
        raw = resp.text.strip()
        if raw.startswith("{") or raw.startswith("["):
            try:
                data = json.loads(raw)
            except Exception:
                data = None
    if not isinstance(data, dict):
        return []

    slug = source.get("ashby_slug") or _extract_board_slug(source["url"])
    direct_jobs: List[Job] = []
    for item in data.get("jobs", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue

        title = normalize_text(item.get("title") or item.get("name") or item.get("jobTitle"))
        if not title:
            continue

        location = _format_location(
            item.get("location")
            or item.get("secondaryLocations")
            or item.get("locationName")
        )
        posted_at = parse_date(
            item.get("publishedAt")
            or item.get("postedAt")
            or item.get("createdAt")
            or item.get("updatedAt")
        )

        url = (
            item.get("jobUrl")
            or item.get("jobPostingUrl")
            or item.get("url")
            or item.get("absoluteUrl")
            or item.get("applyUrl")
            or item.get("jobPostingApplicationLink")
        )
        if not url:
            for key in ("id", "jobId", "jobPostingId", "jobPostingUuid", "slug"):
                value = item.get(key)
                if value:
                    url = f"/{slug}/{str(value).lstrip('/')}"
                    break
        if not url:
            continue

        url = ensure_absolute_url(base_url, url)
        direct_jobs.append(
            Job(
                job_id=hash_job_id(source["name"], url),
                source=source["name"],
                title=title,
                location=location,
                url=url,
                posted_at=posted_at,
                raw=item,
            )
        )

    if direct_jobs:
        return _dedupe_jobs(direct_jobs)

    return _extract_jobs_from_payload(source, base_url, data)


def _extract_jobs_from_links(source: dict, soup: BeautifulSoup, base_url: str) -> List[Job]:
    slug = source.get("ashby_slug") or _extract_board_slug(source["url"])
    if not slug:
        return []

    ignored = {
        "all jobs",
        "view all jobs",
        "learn more",
        "apply",
        "apply now",
    }
    jobs: List[Job] = []
    seen = set()
    slug_segment = f"/{slug}/"

    for link in soup.find_all("a", href=True):
        href = normalize_text(link.get("href"))
        if not href:
            continue
        href_lower = href.lower()
        if slug_segment not in href_lower:
            continue

        title = normalize_text(link.get_text(" ", strip=True))
        if not title:
            title = normalize_text(link.get("aria-label") or link.get("title") or "")
        if not title:
            continue
        if title.lower() in ignored:
            continue

        job_url = ensure_absolute_url(base_url, href)
        job_id = hash_job_id(source["name"], job_url)
        if job_id in seen:
            continue
        seen.add(job_id)
        jobs.append(
            Job(
                job_id=job_id,
                source=source["name"],
                title=title,
                location="",
                url=job_url,
                posted_at=None,
                raw={"source": "ashby_anchor_fallback"},
            )
        )

    return jobs


def fetch_ashby_jobs(source: dict, session) -> List[Job]:
    url = source["url"]
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    page_error = None
    soup = None
    try:
        resp = request_with_retries(session, "GET", url, timeout=30, retries=3)
        soup = BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        page_error = exc
        logger.warning("Ashby HTML fetch failed for %s: %s", source["name"], exc)

    jobs: List[Job] = []

    if soup:
        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            try:
                data = json.loads(script.string)
                jobs.extend(_extract_jobs_from_payload(source, base_url, data))
            except Exception:
                pass

        if jobs:
            return _dedupe_jobs(jobs)

        # Fallback to JSON-LD JobPosting data
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            if not tag.string:
                continue
            for obj in extract_jsonld_objects(tag.string):
                title = normalize_text(obj.get("title"))
                job_url = obj.get("url") or obj.get("hiringOrganization", {}).get("url")
                location = normalize_text(obj.get("jobLocation", {}).get("address", {}).get("addressLocality", ""))
                posted_at = parse_date(obj.get("datePosted"))

                if not title or not job_url:
                    continue

                job_url = ensure_absolute_url(base_url, job_url)
                job_id = hash_job_id(source["name"], job_url)
                jobs.append(
                    Job(
                        job_id=job_id,
                        source=source["name"],
                        title=title,
                        location=location,
                        url=job_url,
                        posted_at=posted_at,
                        raw=obj,
                    )
                )

        if jobs:
            return _dedupe_jobs(jobs)

        anchor_jobs = _extract_jobs_from_links(source, soup, base_url)
        if anchor_jobs:
            return _dedupe_jobs(anchor_jobs)

    try:
        api_jobs = _fetch_ashby_api_jobs(source, session, base_url)
        if api_jobs:
            return api_jobs
    except requests.RequestException as exc:
        logger.warning("Ashby API fallback failed for %s: %s", source["name"], exc)

    if page_error is not None:
        raise page_error

    return _dedupe_jobs(jobs)
