from typing import Dict, List

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import hash_job_id, normalize_text


GET_ROLES_QUERY = """
query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items {
      roleId
      corporateTitle
      jobTitle
      jobFunction
      locations {
        primary
        state
        country
        city
        __typename
      }
      status
      division
      skills
      jobType {
        code
        description
        __typename
      }
      externalSource {
        sourceId
        __typename
      }
      __typename
    }
    __typename
  }
}
""".strip()


def _is_target_highergs_role(title: str) -> bool:
    title_normalized = normalize_text(title).lower()
    if not title_normalized:
        return False

    return (
        "ai engineer" in title_normalized
        or "ai software engineer" in title_normalized
        or "software engineer" in title_normalized
        or "forward deployed engineer" in title_normalized
    )


def _build_location(filters: List[Dict]) -> str:
    if not filters:
        return ""
    primary = next((entry for entry in filters if entry.get("primary")), filters[0])
    city = normalize_text(primary.get("city"))
    state = normalize_text(primary.get("state"))
    country = normalize_text(primary.get("country"))
    pieces = [piece for piece in (city, state, country) if piece]
    return ", ".join(pieces)


def fetch_highergs_jobs(source: dict, session) -> List[Job]:
    api_url = "https://api-higher.gs.com/gateway/api/v1/graphql"
    page_size = int(source.get("highergs_page_size", 20))
    max_pages = int(source.get("max_pages", 30))

    jobs: List[Job] = []
    seen = set()
    total_count = None

    for page_number in range(max_pages):
        payload = {
            "operationName": "GetRoles",
            "variables": {
                "searchQueryInput": {
                    "page": {
                        "pageSize": page_size,
                        "pageNumber": page_number,
                    },
                    "sort": {
                        "sortStrategy": "POSTED_DATE",
                        "sortOrder": "DESC",
                    },
                    "filters": [
                        {
                            "filterCategoryType": "LOCATION",
                            "filters": [
                                {
                                    "filter": "United States",
                                    "subFilters": [],
                                }
                            ],
                        }
                    ],
                    "experiences": ["EARLY_CAREER", "PROFESSIONAL"],
                    "searchTerm": "",
                }
            },
            "query": GET_ROLES_QUERY,
        }

        resp = request_with_retries(
            session,
            "POST",
            api_url,
            json=payload,
            timeout=30,
            retries=3,
        )
        data = (resp.json() or {}).get("data") or {}
        role_search = data.get("roleSearch") or {}
        items = role_search.get("items") or []
        if not items:
            break

        if total_count is None:
            raw_total = role_search.get("totalCount")
            try:
                total_count = int(raw_total)
            except Exception:
                total_count = None

        page_added = 0
        for item in items:
            if not isinstance(item, dict):
                continue

            title = normalize_text(item.get("jobTitle"))
            if not _is_target_highergs_role(title):
                continue

            external_source = item.get("externalSource") or {}
            source_id = normalize_text(external_source.get("sourceId"))
            role_id = normalize_text(item.get("roleId"))
            if not source_id:
                source_id = role_id.split("_", 1)[0] if role_id else ""
            if not source_id:
                continue

            job_url = f"https://higher.gs.com/roles/{source_id}"
            job_id = hash_job_id(source["name"], job_url)
            if job_id in seen:
                continue
            seen.add(job_id)

            location = _build_location(item.get("locations") or [])
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=job_url,
                    posted_at=None,
                    raw=item,
                )
            )
            page_added += 1

        if len(items) < page_size:
            break
        if total_count is not None and (page_number + 1) * page_size >= total_count:
            break
        if page_added == 0 and page_number > 0:
            continue

    return jobs
