import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


SOFTWARE_ENGINEER_PATTERN = re.compile(r"\bsoftware engineer\b", re.I)
SOFTWARE_ENGINEER_EXCLUDE_PATTERN = re.compile(
    r"\b(lead|manager|director|principal|distinguished|architect|head|vice president|vp)\b",
    re.I,
)
PRODUCT_MANAGER_PATTERN = re.compile(r"\b(product manager|product management)\b", re.I)


def _is_target_capitalone_role(title: str) -> bool:
    title_normalized = normalize_text(title)
    if not title_normalized:
        return False

    if PRODUCT_MANAGER_PATTERN.search(title_normalized):
        return True

    if not SOFTWARE_ENGINEER_PATTERN.search(title_normalized):
        return False

    return not SOFTWARE_ENGINEER_EXCLUDE_PATTERN.search(title_normalized)


def _extract_initial_state(source: dict, session) -> Dict[str, str]:
    resp = request_with_retries(session, "GET", source["url"], timeout=30, retries=3)
    soup = BeautifulSoup(resp.text, "lxml")

    results_section = soup.select_one("#search-results")
    if not results_section:
        raise RuntimeError("Capital One search results metadata not found")

    filters_section = soup.select_one("#search-filters")
    sponsorship_filter = soup.select_one('input.filter-checkbox[data-field-name="hours_per_week"][data-id="Sponsored"]')
    if not sponsorship_filter:
        raise RuntimeError("Capital One sponsorship filter not found")

    return {
        "distance": normalize_text(results_section.get("data-distance")) or "50",
        "latitude": normalize_text(results_section.get("data-latitude")),
        "longitude": normalize_text(results_section.get("data-longitude")),
        "location": normalize_text(results_section.get("data-location")) or "United States",
        "show_radius": normalize_text(results_section.get("data-show-radius")) or "False",
        "records_per_page": normalize_text(results_section.get("data-records-per-page")) or "15",
        "custom_facet_name": normalize_text(results_section.get("data-custom-facet-name")),
        "facet_term": normalize_text(results_section.get("data-facet-term")),
        "facet_type": normalize_text(results_section.get("data-facet-type")) or "0",
        "search_results_module_name": normalize_text(results_section.get("data-search-results-module-name")) or "Search Results",
        "search_filters_module_name": normalize_text(filters_section.get("data-search-filters-module-name")) if filters_section else "Search Filters",
        "sort_criteria": normalize_text(results_section.get("data-sort-criteria")) or "0",
        "sort_direction": normalize_text(results_section.get("data-sort-direction")) or "0",
        "search_type": normalize_text(results_section.get("data-search-type")) or "1",
        "location_type": normalize_text(results_section.get("data-location-type")) or "2",
        "location_path": normalize_text(results_section.get("data-location-path")),
        "organization_ids": normalize_text(results_section.get("data-organization-ids")),
        "postal_code": normalize_text(results_section.get("data-postal-code")),
        "results_type": normalize_text(results_section.get("data-results-type")) or "0",
        "radius_unit_type": normalize_text(filters_section.get("data-radius-unit-type")) if filters_section else "0",
        "facet_id": normalize_text(sponsorship_filter.get("data-id")) or "Sponsored",
        "facet_count": normalize_text(sponsorship_filter.get("data-count")) or "",
        "facet_display": normalize_text(sponsorship_filter.get("data-display")) or "Sponsored",
        "facet_field_name": normalize_text(sponsorship_filter.get("data-field-name")) or "hours_per_week",
        "facet_filter_type": normalize_text(sponsorship_filter.get("data-facet-type")) or "5",
    }


def _build_results_params(state: Dict[str, str], *, page: int) -> Dict[str, str]:
    return {
        "ActiveFacetID": state["facet_id"],
        "CurrentPage": str(page),
        "RecordsPerPage": state["records_per_page"],
        "TotalContentResults": "",
        "Distance": state["distance"],
        "RadiusUnitType": state["radius_unit_type"] or "0",
        "Keywords": "",
        "Location": state["location"],
        "Latitude": state["latitude"],
        "Longitude": state["longitude"],
        "ShowRadius": state["show_radius"],
        "IsPagination": "true" if page > 1 else "false",
        "CustomFacetName": state["custom_facet_name"],
        "FacetTerm": state["facet_term"],
        "FacetType": state["facet_type"],
        "FacetFilters[0].ID": state["facet_id"],
        "FacetFilters[0].FacetType": state["facet_filter_type"],
        "FacetFilters[0].Count": state["facet_count"],
        "FacetFilters[0].Display": state["facet_display"],
        "FacetFilters[0].IsApplied": "true",
        "FacetFilters[0].FieldName": state["facet_field_name"],
        "SearchResultsModuleName": state["search_results_module_name"],
        "SearchFiltersModuleName": state["search_filters_module_name"] or "Search Filters",
        "SortCriteria": state["sort_criteria"],
        "SortDirection": state["sort_direction"],
        "SearchType": state["search_type"],
        "LocationType": state["location_type"],
        "LocationPath": state["location_path"],
        "OrganizationIds": state["organization_ids"],
        "PostalCode": state["postal_code"],
        "ResultsType": state["results_type"],
        "fc": "",
        "fl": "",
        "fcf": "",
        "afc": "",
        "afl": "",
        "afcf": "",
        "TotalContentPages": "NaN",
    }


def _parse_results_page(source: dict, html: str, base_url: str, seen: set) -> tuple[List[Job], Optional[int]]:
    soup = BeautifulSoup(html, "lxml")
    jobs: List[Job] = []

    results_section = soup.select_one("#search-results")
    total_pages = None
    if results_section:
        raw_total_pages = normalize_text(results_section.get("data-total-pages"))
        if raw_total_pages.isdigit():
            total_pages = int(raw_total_pages)

    for link in soup.select("#search-results-list a[data-job-id]"):
        href = normalize_text(link.get("href"))
        title_el = link.find("h2")
        title = normalize_text(title_el.get_text()) if title_el else normalize_text(link.get_text(" ", strip=True))
        if not href or not title or not _is_target_capitalone_role(title):
            continue

        job_url = ensure_absolute_url(base_url, href)
        job_id = hash_job_id(source["name"], job_url)
        if job_id in seen:
            continue
        seen.add(job_id)

        location_el = link.select_one(".job-location")
        posted_el = link.select_one(".job-date-posted")
        location = normalize_text(location_el.get_text()) if location_el else ""
        posted_at = parse_date(posted_el.get_text()) if posted_el else None

        jobs.append(
            Job(
                job_id=job_id,
                source=source["name"],
                title=title,
                location=location,
                url=job_url,
                posted_at=posted_at,
                raw={"sponsored": True, "capitalone_job_id": normalize_text(link.get("data-job-id"))},
            )
        )

    return jobs, total_pages


def fetch_capitalone_jobs(source: dict, session) -> List[Job]:
    parsed = urlparse(source["url"])
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    state = _extract_initial_state(source, session)
    page_size = int(state["records_per_page"] or 15)
    max_pages = int(source.get("max_pages", 30))
    results_url = ensure_absolute_url(base_url, "/search-jobs/results")

    jobs: List[Job] = []
    seen = set()
    total_pages = None

    for page in range(1, max_pages + 1):
        params = _build_results_params(state, page=page)
        resp = request_with_retries(session, "GET", results_url, params=params, timeout=30, retries=3)
        payload = resp.json() or {}
        results_html = payload.get("results")
        if not isinstance(results_html, str) or not results_html.strip():
            break

        page_jobs, page_total_pages = _parse_results_page(source, results_html, base_url, seen)
        if not page_jobs and page > 1:
            break
        jobs.extend(page_jobs)

        if total_pages is None and page_total_pages:
            total_pages = page_total_pages
        if total_pages is not None and page >= total_pages:
            break
        if len(page_jobs) < page_size:
            break

    return jobs
