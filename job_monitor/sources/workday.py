from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


class WorkdayConfigError(RuntimeError):
    pass


def extract_workday_parts(url: str) -> dict:
    parsed = urlparse(url)
    host = parsed.netloc
    if not host:
        raise WorkdayConfigError(f"Invalid Workday URL: {url}")

    tenant = host.split(".")[0]
    parts = [p for p in parsed.path.split("/") if p]

    locale: Optional[str] = None
    site: Optional[str] = None
    for part in parts:
        if "-" in part and part.lower().startswith("en"):
            locale = part
            continue
        if not site:
            site = part

    if not site:
        raise WorkdayConfigError(f"Unable to determine Workday site from {url}")

    query = parse_qs(parsed.query)
    return {
        "host": host,
        "tenant": tenant,
        "site": site,
        "locale": locale or "en-US",
        "query": query,
    }


def extract_items(data: dict) -> list:
    for key in ("jobPostings", "items", "jobs"):
        if isinstance(data.get(key), list):
            return data[key]

    nested = data.get("data")
    if isinstance(nested, dict):
        for key in ("jobPostings", "items", "jobs"):
            if isinstance(nested.get(key), list):
                return nested[key]
    elif isinstance(nested, list):
        return nested

    return []


def build_job_url(host: str, locale: str, site: str, item: dict) -> Optional[str]:
    candidate = item.get("externalPath") or item.get("externalUrl") or item.get("applyUrl")
    if candidate:
        return ensure_absolute_url(f"https://{host}", candidate)

    job_posting_id = item.get("jobPostingId") or item.get("jobPostingID")
    if job_posting_id:
        return f"https://{host}/{locale}/{site}/job/{job_posting_id}"

    return None


def format_location(item: dict) -> str:
    loc = item.get("locationsText") or item.get("location")
    if isinstance(loc, str):
        return normalize_text(loc)
    if isinstance(loc, list):
        return normalize_text(", ".join([l for l in loc if isinstance(l, str)]))
    return ""


def fetch_workday_jobs(source: dict, session) -> List[Job]:
    parts = extract_workday_parts(source["url"])
    host = parts["host"]
    tenant = source.get("workday_tenant") or parts["tenant"]
    site = source.get("workday_site") or parts["site"]
    locale = source.get("workday_locale") or parts["locale"]

    api_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

    query = parts["query"]
    search_text = None
    if query.get("q"):
        search_text = query.get("q")[0]

    applied_facets = _extract_applied_facets(query)
    if source.get("workday_ignore_query_facets"):
        applied_facets = {}
    sort_by = None
    if query.get("sortBy"):
        sort_by = query.get("sortBy")[0]

    jobs: List[Job] = []
    offset = 0
    limit = int(source.get("workday_limit", 50))

    while True:
        params = {
            "limit": limit,
            "offset": offset,
            "applyUrlTemplate": "true",
        }
        if search_text:
            params["searchText"] = search_text
            params["keyword"] = search_text
        for key, values in applied_facets.items():
            params[key] = values

        try:
            data = _request_workday_data(
                session,
                api_url,
                params,
                search_text,
                applied_facets,
                sort_by,
                locale,
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                html_jobs = _fetch_workday_jobs_html(source, session, host)
                if html_jobs:
                    return html_jobs
            raise

        items = extract_items(data)
        if not items:
            break

        for item in items:
            title = normalize_text(item.get("title") or item.get("jobTitle"))
            url = build_job_url(host, locale, site, item)
            location = format_location(item)
            posted_at = parse_date(item.get("postedOn") or item.get("postedDate") or item.get("startDate"))

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
        total = data.get("total") or data.get("totalFound")
        if total is not None and offset >= int(total):
            break

    return jobs


def _fetch_workday_jobs_html(source: dict, session, host: str) -> List[Job]:
    url = source["url"]
    resp = request_with_retries(session, "GET", url, timeout=20)
    soup = BeautifulSoup(resp.text, "lxml")

    base_url = f"https://{host}"
    jobs: List[Job] = []
    seen = set()

    cards = soup.select('[data-automation-id="jobResult"]')
    if cards:
        for card in cards:
            link = card.find("a", attrs={"data-automation-id": "jobTitle"}) or card.find("a", href=True)
            if not link:
                continue
            href = link.get("href")
            title = normalize_text(link.get_text())
            if not href or not title:
                continue

            job_url = ensure_absolute_url(base_url, href)
            if job_url in seen:
                continue
            seen.add(job_url)

            location_el = card.find(attrs={"data-automation-id": "locations"})
            date_el = card.find(attrs={"data-automation-id": "postedOn"})
            location = normalize_text(location_el.get_text()) if location_el else ""
            posted_at = parse_date(date_el.get_text()) if date_el else None

            job_id = hash_job_id(source["name"], job_url)
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=job_url,
                    posted_at=posted_at,
                    raw={"source": "workday_html"},
                )
            )

    if jobs:
        return jobs

    for link in soup.find_all("a", href=True):
        href = link.get("href")
        if not href or "/job/" not in href:
            continue
        title = normalize_text(link.get_text())
        if not title:
            continue

        job_url = ensure_absolute_url(base_url, href)
        if job_url in seen:
            continue
        seen.add(job_url)

        parent = link.find_parent()
        location = ""
        posted_at = None
        if parent:
            location_el = parent.find(attrs={"data-automation-id": "locations"})
            date_el = parent.find(attrs={"data-automation-id": "postedOn"})
            location = normalize_text(location_el.get_text()) if location_el else ""
            posted_at = parse_date(date_el.get_text()) if date_el else None

        job_id = hash_job_id(source["name"], job_url)
        jobs.append(
            Job(
                job_id=job_id,
                source=source["name"],
                title=title,
                location=location,
                url=job_url,
                posted_at=posted_at,
                raw={"source": "workday_html_fallback"},
            )
        )

    return jobs


def _request_workday_data(
    session,
    api_url: str,
    params: dict,
    search_text: Optional[str],
    applied_facets: dict,
    sort_by: Optional[str],
    locale: str,
) -> dict:
    try:
        params["locale"] = locale
        resp = session.get(api_url, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 400:
            raise

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "limit": params.get("limit"),
        "offset": params.get("offset"),
        "searchText": search_text or "",
        "appliedFacets": applied_facets or {},
        "locale": locale,
    }
    if not payload.get("searchText"):
        payload.pop("searchText", None)
    if not payload.get("appliedFacets"):
        payload.pop("appliedFacets", None)
    if sort_by:
        payload["sortBy"] = sort_by

    resp = session.post(api_url, params={"locale": locale}, json=payload, headers=headers, timeout=20)
    if resp.status_code == 400:
        # Retry without search text
        payload.pop("searchText", None)
        resp = session.post(api_url, params={"locale": locale}, json=payload, headers=headers, timeout=20)

    if resp.status_code == 400 and payload.get("appliedFacets"):
        # Last-resort fallback: remove facets entirely
        payload.pop("appliedFacets", None)
        resp = session.post(api_url, params={"locale": locale}, json=payload, headers=headers, timeout=20)

    if resp.status_code == 400:
        raise requests.HTTPError(resp.text, response=resp)

    resp.raise_for_status()
    return resp.json()


def _extract_applied_facets(query: dict) -> dict:
    applied = {}
    ignored = {
        "q",
        "searchText",
        "keyword",
        "sortBy",
        "mode",
        "lastSelectedFacet",
        "selectedPostingDatesFacet",
        "sort_by",
    }
    for key, values in query.items():
        if key in ignored:
            continue
        if not values:
            continue
        applied[key] = values
    return applied
