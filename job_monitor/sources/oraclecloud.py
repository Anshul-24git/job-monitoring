import re
from typing import List
from urllib.parse import parse_qs, urlparse

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import hash_job_id, normalize_text, parse_date


_SITE_NUMBER_RE = re.compile(r"siteNumber:\s*'([^']+)'")
_PATH_RE = re.compile(r"/hcmUI/CandidateExperience/([^/]+)/sites/([^/]+)/")
_FACETS = "LOCATIONS;WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;POSTING_DATES;FLEX_FIELDS"
_EXPAND = (
    "requisitionList.workLocation,"
    "requisitionList.otherWorkLocations,"
    "requisitionList.secondaryLocations,"
    "flexFieldsFacet.values,"
    "requisitionList.requisitionFlexFields"
)


def _extract_site_number(html: str) -> str:
    match = _SITE_NUMBER_RE.search(html)
    if not match:
        raise RuntimeError("Oracle Cloud site number not found")
    return match.group(1)


def _extract_site_parts(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    match = _PATH_RE.search(parsed.path + "/")
    if not match:
        raise RuntimeError(f"Unable to determine Candidate Experience site from {url}")
    return parsed.netloc, match.group(1), match.group(2)


def _build_job_url(host: str, locale: str, site: str, job_id: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/{locale}/sites/{site}/job/{job_id}/"


def _build_finder(site_number: str, *, limit: int, offset: int, keyword: str, location_id: str, sort_by: str) -> str:
    parts = [
        f"siteNumber={site_number}",
        f"facetsList={_FACETS}",
        f"limit={limit}",
        f"offset={offset}",
    ]
    if keyword:
        escaped = keyword.replace('\"', '\\"')
        parts.append(f'keyword=\"{escaped}\"')
    if location_id:
        parts.append(f"locationId={location_id}")
    if sort_by:
        parts.append(f"sortBy={sort_by}")
    return "findReqs;" + ",".join(parts)


def fetch_oraclecloud_jobs(source: dict, session) -> List[Job]:
    host, locale, site = _extract_site_parts(source["url"])
    parsed = urlparse(source["url"])
    query = parse_qs(parsed.query)

    keyword = normalize_text((query.get("keyword") or [""])[0])
    location_id = normalize_text((query.get("locationId") or [""])[0])
    sort_by = normalize_text((query.get("sortBy") or ["POSTING_DATES_DESC"])[0]) or "POSTING_DATES_DESC"
    limit = int(source.get("oraclecloud_limit", 25))
    max_pages = int(source.get("max_pages", 60))

    search_page = request_with_retries(session, "GET", source["url"], timeout=30, retries=3)
    site_number = source.get("oraclecloud_site_number") or _extract_site_number(search_page.text)

    api_url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    jobs: List[Job] = []
    seen = set()
    offset = 0
    total_jobs = None

    for _ in range(max_pages):
        finder = _build_finder(
            site_number,
            limit=limit,
            offset=offset,
            keyword=keyword,
            location_id=location_id,
            sort_by=sort_by,
        )
        resp = request_with_retries(
            session,
            "GET",
            api_url,
            params={
                "onlyData": "true",
                "expand": _EXPAND,
                "finder": finder,
            },
            timeout=30,
            retries=3,
        )
        payload = resp.json() or {}
        items = payload.get("items") or []
        if not items:
            break

        search_result = items[0] if isinstance(items[0], dict) else {}
        requisitions = search_result.get("requisitionList") or []
        if not requisitions:
            break

        if total_jobs is None:
            raw_total = search_result.get("TotalJobsCount")
            try:
                total_jobs = int(raw_total)
            except Exception:
                total_jobs = None

        for req in requisitions:
            if not isinstance(req, dict):
                continue

            job_id_raw = normalize_text(req.get("Id"))
            title = normalize_text(req.get("Title"))
            if not job_id_raw or not title:
                continue

            job_url = _build_job_url(host, locale, site, job_id_raw)
            job_id = hash_job_id(source["name"], job_url)
            if job_id in seen:
                continue
            seen.add(job_id)

            location = normalize_text(req.get("PrimaryLocation"))
            posted_at = parse_date(req.get("PostedDate"))

            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=job_url,
                    posted_at=posted_at,
                    raw=req,
                )
            )

        offset += len(requisitions)
        if len(requisitions) < limit:
            break
        if total_jobs is not None and offset >= total_jobs:
            break

    return jobs
