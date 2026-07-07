import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import Job
from ..http_utils import request_with_retries
from ..utils import ensure_absolute_url, extract_jsonld_objects, hash_job_id, normalize_text, parse_date

TITLE_KEYS = ("title", "jobTitle", "name", "positionTitle", "posting_name")
URL_KEYS = (
    "url",
    "jobUrl",
    "applyUrl",
    "externalUrl",
    "jobPostingUrl",
    "postingUrl",
    "detailUrl",
    "jobDetailUrl",
    "canonicalPositionUrl",
)
LOCATION_KEYS = ("location", "locationsText", "primaryLocation", "jobLocation", "city")
DATE_KEYS = ("datePosted", "postedAt", "postingDate", "updatedAt", "createdAt", "publishedAt", "t_update", "t_create", "timestamp")
EMBEDDED_STATE_MARKERS = (
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
    "__NUXT__",
    "__APOLLO_STATE__",
)
NAVIGATION_TITLES = {
    "careers",
    "uber careers",
    "business development",
    "communications",
    "community operations",
    "design",
    "emerging talent",
    "engineering",
    "finance",
    "insurance",
    "marketing",
    "people & places",
    "product",
}
JOB_LINK_PATTERNS = (
    "/job",
    "jobid=",
    "gh_jid",
    "/careers/list/",
)
TITLE_LOCATION_RE = re.compile(r"^(?P<title>.+?)\s+Location:\s*(?P<location>.+?)(?:\s+Team:\s*.+)?$", re.I)
LOCATION_LABELED_RE = re.compile(r"\bLocation:\s*(?P<location>.+?)(?:\s+Team:|$)", re.I)
POSTED_LABELED_RE = re.compile(r"\b(?:posted|date posted|posted on|updated):\s*(?P<value>.+)$", re.I)
DATE_TEXT_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\b"
)
RELATIVE_AGE_RE = re.compile(r"\b(?P<num>\d+)\s*(?P<unit>d|day|days|h|hour|hours|m|min|mins|minute|minutes)\b", re.I)
LOCATION_LINE_RE = re.compile(
    r"\b(remote|united states|usa|u\.s\.|[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}\b|"
    r"[A-Z][A-Za-z .'-]+,\s*[A-Za-z][A-Za-z .'-]+|"
    r"United States\s*-\s*[A-Za-z .'-]+\s*-\s*[A-Za-z .'-]+)\b",
    re.I,
)


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
    title, inferred_location = _extract_title_and_location(title, url)
    if not _is_probable_job_record(source, title, url):
        return None

    location = ""
    for key in LOCATION_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            location = normalize_text(value)
            break
    if not location and isinstance(item.get("jobLocation"), dict):
        location = extract_location_from_jsonld({"jobLocation": item.get("jobLocation")})
    if not location and inferred_location:
        location = inferred_location

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


def _is_probable_job_record(source: dict, title: str, url: str) -> bool:
    title_l = normalize_text(title).lower()
    if title_l in NAVIGATION_TITLES:
        return False

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if path.rstrip("/") in {"", "/jobs", "/careers", "/positions"}:
        return False

    if "uber.com" in host:
        if "/careers/list/" not in path:
            return False
        parts = [part for part in path.rstrip("/").split("/") if part]
        tail = parts[-1] if parts else ""
        if not tail.isdigit():
            return False

    return True


def _request_settings(source: dict) -> tuple[int, int, float]:
    timeout = int(source.get("timeout_seconds", 20))
    retries = int(source.get("request_retries", 2))
    backoff = float(source.get("request_backoff_seconds", 1.0))

    host = urlparse(source["url"]).netloc.lower()
    if "uber.com" in host:
        timeout = max(timeout, 30)
        retries = max(retries, 4)

    return timeout, retries, backoff


def _append_jobs_from_payload(source: dict, base_url: str, payload: Any, jobs: List[Job], seen_ids: set) -> None:
    for item in iter_candidate_jobs(payload):
        job = parse_candidate_job(source, base_url, item)
        if not job:
            continue
        if job.job_id in seen_ids:
            continue
        seen_ids.add(job.job_id)
        jobs.append(job)


def _decode_json_from_text(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_payload_after_marker(script_text: str, marker: str) -> Any:
    marker_idx = script_text.find(marker)
    if marker_idx == -1:
        return None

    decoder = json.JSONDecoder()
    for token in ("{", "["):
        start = script_text.find(token, marker_idx)
        if start == -1:
            continue
        try:
            payload, _ = decoder.raw_decode(script_text[start:])
            return payload
        except Exception:
            continue
    return None


def _infer_location_from_job_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "job":
        return ""
    slug = normalize_text(parts[1]).replace("-", " ")
    return slug.title()


def _infer_title_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    slug = parts[-1]
    slug = re.sub(r"-[a-z0-9]{8,}$", "", slug, flags=re.I)
    return normalize_text(slug.replace("-", " ")).title()


def _extract_title_and_location(title: str, url: str) -> tuple[str, str]:
    normalized = normalize_text(title)
    if not normalized:
        return "", ""

    location = ""
    match = TITLE_LOCATION_RE.match(normalized)
    if match:
        normalized = normalize_text(match.group("title"))
        location = normalize_text(match.group("location"))

    if not location:
        location = _infer_location_from_job_url(url)

    if location:
        normalized = re.sub(rf"[\s,\-]+{re.escape(location)}$", "", normalized, flags=re.I)
        normalized = normalize_text(normalized)

    return normalized, location


def _split_text_lines(text: str) -> List[str]:
    lines = []
    for part in re.split(r"[\r\n]+", text or ""):
        normalized = normalize_text(part)
        if normalized:
            lines.append(normalized)
    return lines


def _extract_context_location(lines: List[str], title: str) -> str:
    for line in lines:
        if normalize_text(line).lower() == normalize_text(title).lower():
            continue
        match = LOCATION_LABELED_RE.search(line)
        if match:
            return normalize_text(match.group("location"))
        if LOCATION_LINE_RE.search(line):
            return normalize_text(line)
    return ""


def _extract_context_posted(lines: List[str]) -> tuple[str, bool]:
    for line in lines:
        match = POSTED_LABELED_RE.search(line)
        if match:
            value = normalize_text(match.group("value"))
            return value, bool(DATE_TEXT_RE.search(value) and ":" not in value and "T" not in value and "t" not in value)
        match = DATE_TEXT_RE.search(line)
        if match:
            value = normalize_text(match.group(0))
            return value, True
    return "", False


def _parse_posted_at_text(value: str):
    if normalize_text(value).lower() == "new":
        return datetime.now(timezone.utc)

    parsed = parse_date(value)
    if parsed:
        return parsed

    match = RELATIVE_AGE_RE.search(normalize_text(value))
    if not match:
        return None

    amount = int(match.group("num"))
    unit = match.group("unit").lower()
    if unit.startswith("d"):
        delta = timedelta(days=amount)
    elif unit.startswith("h"):
        delta = timedelta(hours=amount)
    else:
        delta = timedelta(minutes=amount)
    return datetime.now(timezone.utc) - delta


def _find_candidate_card(link) -> Any:
    node = link
    for _ in range(6):
        node = getattr(node, "parent", None)
        if node is None or getattr(node, "name", None) is None:
            break
        if node.name not in {"article", "li", "div", "section", "tr"}:
            continue
        text = normalize_text(node.get_text(" ", strip=True))
        if len(text) >= 24:
            return node
    return None


def _extract_context_from_link(link, title: str) -> tuple[str, str, bool]:
    card = _find_candidate_card(link)
    if card is None:
        return "", "", False
    lines = _split_text_lines(card.get_text("\n", strip=True))
    location = _extract_context_location(lines, title)
    posted_text, posted_at_is_date_only = _extract_context_posted(lines)
    return location, posted_text, posted_at_is_date_only


def _collect_jsonld_jobpostings(source: dict, soup: BeautifulSoup, base_url: str) -> dict[str, dict]:
    postings: dict[str, dict] = {}
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        for obj in extract_jsonld_objects(tag.string):
            title = normalize_text(obj.get("title"))
            link = obj.get("url")
            if not title or not link:
                continue
            link = ensure_absolute_url(base_url, link)
            postings[link] = {
                "title": title,
                "location": extract_location_from_jsonld(obj),
                "posted_at": parse_date(obj.get("datePosted")),
                "posted_value": obj.get("datePosted"),
            }
    return postings


def _enrich_jobs_from_jsonld(jobs: List[Job], jsonld_jobs: dict[str, dict]) -> None:
    for job in jobs:
        data = jsonld_jobs.get(job.url)
        if not data:
            continue
        if not job.location and data.get("location"):
            job.location = data["location"]
        if job.posted_at is None and data.get("posted_at"):
            job.posted_at = data["posted_at"]
        raw = job.raw if isinstance(job.raw, dict) else {}
        if data.get("posted_value") and not raw.get("posted_at_text"):
            raw["posted_at_text"] = data["posted_value"]
            raw["posted_at_is_date_only"] = bool(
                DATE_TEXT_RE.search(str(data["posted_value"]))
                and ":" not in str(data["posted_value"])
                and "T" not in str(data["posted_value"])
                and "t" not in str(data["posted_value"])
            )
        if (" Location:" in job.title or "+1 locations" in job.title) and data.get("title"):
            job.title = data["title"]
        job.raw = raw


def _is_jobposting_type(value: Any) -> bool:
    if isinstance(value, list):
        return any(_is_jobposting_type(item) for item in value)
    return normalize_text(value).lower() == "jobposting"


def _is_date_only_text(value: Any) -> bool:
    text = normalize_text(value)
    if not text:
        return False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00(?:\.0+)?Z?", text, re.I):
        return True
    return bool(DATE_TEXT_RE.search(text) and ":" not in text and "T" not in text and "t" not in text)


def _extract_detail_jobposting(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, Any] = {}

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        for obj in extract_jsonld_objects(tag.string):
            if not isinstance(obj, dict) or not _is_jobposting_type(obj.get("@type")):
                continue
            title = normalize_text(obj.get("title"))
            location = extract_location_from_jsonld(obj)
            posted_value = obj.get("datePosted")
            posted_at = parse_date(posted_value)
            if title:
                result["title"] = title
            if location:
                result["location"] = location
            if posted_value:
                result["posted_value"] = posted_value
            if posted_at:
                result["posted_at"] = posted_at
            if result:
                return result

    date_el = soup.select_one('[itemprop="datePosted"]')
    if date_el:
        posted_value = normalize_text(date_el.get_text(" ", strip=True))
        posted_at = parse_date(posted_value)
        if posted_value:
            result["posted_value"] = posted_value
        if posted_at:
            result["posted_at"] = posted_at

    location_el = soup.select_one('[itemprop="jobLocation"]')
    if location_el:
        location = normalize_text(location_el.get_text(" ", strip=True))
        location = re.sub(r"^Location\s+", "", location, flags=re.I)
        if location:
            result["location"] = location

    return result


def _enrich_jobs_from_detail_pages(source: dict, session, jobs: List[Job], *, timeout: int) -> None:
    if not source.get("detail_jsonld_enrich"):
        return

    max_jobs = int(source.get("detail_jsonld_max_jobs", len(jobs)) or 0)
    detail_timeout = int(source.get("detail_timeout_seconds", timeout) or timeout)
    retries = int(source.get("detail_retries", 1) or 0)
    update_title = bool(source.get("detail_update_title", False))
    preserve_date_only = bool(source.get("preserve_date_only_posted_at", False))

    for job in jobs[:max(max_jobs, 0)]:
        try:
            resp = request_with_retries(
                session,
                "GET",
                job.url,
                timeout=detail_timeout,
                retries=retries,
                backoff_seconds=0.5,
            )
            detail = _extract_detail_jobposting(resp.text, job.url)
        except Exception:
            continue

        if update_title and detail.get("title"):
            job.title = normalize_text(detail["title"])
        if detail.get("location"):
            job.location = normalize_text(detail["location"])
        if detail.get("posted_at"):
            job.posted_at = detail["posted_at"]

        raw = job.raw if isinstance(job.raw, dict) else {}
        if detail.get("posted_value"):
            raw["posted_at_text"] = detail["posted_value"]
            raw["posted_at_is_date_only"] = _is_date_only_text(detail["posted_value"])
        if preserve_date_only:
            raw["preserve_date_only_posted_at"] = True
        job.raw = raw


def _finalize_jobs(source: dict, session, jobs: List[Job], jsonld_jobs: dict[str, dict], *, timeout: int) -> List[Job]:
    _enrich_jobs_from_jsonld(jobs, jsonld_jobs)
    _enrich_jobs_from_detail_pages(source, session, jobs, timeout=timeout)
    return jobs


def _extract_jobs_from_links(source: dict, soup: BeautifulSoup, base_url: str) -> List[Job]:
    ignored_titles = {
        "apply",
        "apply now",
        "learn more",
        "read more",
        "view all jobs",
        "all jobs",
        "search jobs",
    }
    ignored_titles |= {normalize_text(value).lower() for value in source.get("ignored_link_titles", [])}
    infer_title_values = {
        normalize_text(value).lower()
        for value in source.get("infer_title_from_url_when_titles", [])
        if normalize_text(value)
    }
    job_link_patterns = source.get("job_link_patterns") or JOB_LINK_PATTERNS
    job_link_patterns = [normalize_text(pattern).lower() for pattern in job_link_patterns if normalize_text(pattern)]
    job_link_exclude_patterns = [
        normalize_text(pattern).lower()
        for pattern in source.get("job_link_exclude_patterns", [])
        if normalize_text(pattern)
    ]
    jobs: List[Job] = []
    seen_ids = set()

    for link in soup.find_all("a", href=True):
        href = normalize_text(link.get("href"))
        if not href:
            continue
        href_l = href.lower()
        if not any(pattern in href_l for pattern in job_link_patterns):
            continue
        if job_link_exclude_patterns and any(pattern in href_l for pattern in job_link_exclude_patterns):
            continue

        title = normalize_text(link.get_text(" ", strip=True))
        if not title:
            title = normalize_text(link.get("aria-label") or link.get("title") or "")
        if not title:
            continue
        if title.lower() in ignored_titles:
            continue
        if len(title) < 6:
            continue

        url = ensure_absolute_url(base_url, href)
        if title.lower() in infer_title_values:
            inferred_title = _infer_title_from_url(url)
            if inferred_title:
                title = inferred_title
        title, location = _extract_title_and_location(title, url)
        if not title:
            continue
        context_location, posted_text, posted_at_is_date_only = _extract_context_from_link(link, title)
        if not location and context_location:
            location = context_location
        if not posted_text and source.get("infer_relative_age_from_title"):
            age_match = RELATIVE_AGE_RE.search(title)
            if age_match:
                posted_text = age_match.group(0)
                posted_at_is_date_only = False
            elif re.search(r"\bnew\b", title, re.I):
                posted_text = "new"
                posted_at_is_date_only = False
        if not _is_probable_job_record(source, title, url):
            continue
        job_id = hash_job_id(source["name"], url)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        jobs.append(
            Job(
                job_id=job_id,
                source=source["name"],
                title=title,
                location=location,
                url=url,
                posted_at=_parse_posted_at_text(posted_text) if posted_text else None,
                raw={
                    "source": "anchor_fallback",
                    "posted_at_text": posted_text,
                    "posted_at_is_date_only": posted_at_is_date_only,
                    "posted_at_display_label": "seen" if normalize_text(posted_text).lower() == "new" else "",
                },
            )
        )

    return jobs


def _collect_json_nodes(payload: Any) -> Iterable[dict]:
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _collect_json_nodes(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _collect_json_nodes(item)


def _extract_jobs_from_jsonld_itemlists(source: dict, base_url: str, raw_text: str, seen_ids: set) -> List[Job]:
    payload = _decode_json_from_text(raw_text)
    if payload is None:
        return []

    jobs: List[Job] = []
    for node in _collect_json_nodes(payload):
        node_type = node.get("@type")
        if node_type != "ItemList":
            continue
        items = node.get("itemListElement")
        if not isinstance(items, list):
            continue

        for elem in items:
            candidate = elem
            if isinstance(elem, dict) and isinstance(elem.get("item"), dict):
                candidate = elem["item"]
            if not isinstance(candidate, dict):
                continue

            title = normalize_text(
                candidate.get("title")
                or candidate.get("name")
                or (elem.get("name") if isinstance(elem, dict) else "")
            )
            link = candidate.get("url") or (elem.get("url") if isinstance(elem, dict) else "")
            if not title or not link:
                continue

            link = ensure_absolute_url(base_url, str(link))
            title, location = _extract_title_and_location(title, link)
            if not _is_probable_job_record(source, title, link):
                continue
            job_id = hash_job_id(source["name"], link)
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=link,
                    posted_at=None,
                    raw={"source": "jsonld_itemlist"},
                )
            )

    return jobs


def fetch_generic_jobs(source: dict, session) -> List[Job]:
    url = source["url"]
    timeout, retries, backoff = _request_settings(source)
    resp = request_with_retries(
        session,
        "GET",
        url,
        timeout=timeout,
        retries=retries,
        backoff_seconds=backoff,
    )
    resp.raise_for_status()
    html = resp.text
    if len(html) < 1200:
        redirect_match = re.search(
            r"""<a[^>]+href=["']([^"']+)["'][^>]*>\s*(?:here|continue|redirect)\s*</a>""",
            html,
            re.I,
        )
        if redirect_match:
            next_url = ensure_absolute_url(url, redirect_match.group(1))
            resp = request_with_retries(
                session,
                "GET",
                next_url,
                timeout=timeout,
                retries=retries,
                backoff_seconds=backoff,
            )
            resp.raise_for_status()
            html = resp.text

    soup = BeautifulSoup(html, "lxml")
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    jsonld_jobs = _collect_jsonld_jobpostings(source, soup, base_url)

    jobs: List[Job] = []
    seen_ids = set()

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
            location_parts = selectors.get("location_parts") or []
            if not location and location_parts:
                if isinstance(location_parts, str):
                    location_parts = [location_parts]
                parts = []
                for selector in location_parts:
                    for part_el in card.select(selector):
                        part = normalize_text(part_el.get_text())
                        if part:
                            parts.append(part)
                location = normalize_text(", ".join(parts))
            posted_text = normalize_text(date_el.get_text()) if date_el else ""
            posted_at = _parse_posted_at_text(posted_text) if posted_text else None
            link = link_el.get("href") if link_el else None

            if not title or not link:
                continue

            context_location, context_posted_text, context_date_only = _extract_context_from_link(link_el, title) if link_el else ("", "", False)
            if not location and context_location:
                location = context_location
            if not posted_text and context_posted_text:
                posted_text = context_posted_text
                posted_at = _parse_posted_at_text(posted_text) if posted_text else None

            link = ensure_absolute_url(base_url, link)
            title, inferred_location = _extract_title_and_location(title, link)
            if not location and inferred_location:
                location = inferred_location
            job_id = hash_job_id(source["name"], link)
            seen_ids.add(job_id)
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=location,
                    url=link,
                    posted_at=posted_at,
                    raw={
                        "selector": job_card_selector,
                        "posted_at_text": posted_text,
                        "posted_at_is_date_only": bool(
                            posted_text and (
                                context_date_only
                                or (DATE_TEXT_RE.search(posted_text) and ":" not in posted_text and "T" not in posted_text and "t" not in posted_text)
                            )
                        ),
                        "preserve_date_only_posted_at": bool(source.get("preserve_date_only_posted_at", False)),
                    },
                )
            )

    if jobs:
        return _finalize_jobs(source, session, jobs, jsonld_jobs, timeout=timeout)

    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            data = json.loads(next_data.string)
            _append_jobs_from_payload(source, base_url, data, jobs, seen_ids)
        except Exception:
            pass

    if jobs:
        return _finalize_jobs(source, session, jobs, jsonld_jobs, timeout=timeout)

    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        if not tag.string:
            continue
        payload = _decode_json_from_text(tag.string)
        if payload is None:
            continue
        _append_jobs_from_payload(source, base_url, payload, jobs, seen_ids)

    if jobs:
        return _finalize_jobs(source, session, jobs, jsonld_jobs, timeout=timeout)

    for tag in soup.find_all("script"):
        script_text = tag.string or ""
        if not script_text:
            continue
        for marker in EMBEDDED_STATE_MARKERS:
            payload = _extract_payload_after_marker(script_text, marker)
            if payload is None:
                continue
            _append_jobs_from_payload(source, base_url, payload, jobs, seen_ids)
            if jobs:
                break
        if jobs:
            break

    if jobs:
        return _finalize_jobs(source, session, jobs, jsonld_jobs, timeout=timeout)

    for tag in soup.find_all("code"):
        raw_text = tag.string or tag.get_text()
        if not raw_text:
            continue
        payload = _decode_json_from_text(raw_text)
        if payload is None:
            continue
        _append_jobs_from_payload(source, base_url, payload, jobs, seen_ids)

    if jobs:
        return _finalize_jobs(source, session, jobs, jsonld_jobs, timeout=timeout)

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
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
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

        itemlist_jobs = _extract_jobs_from_jsonld_itemlists(source, base_url, tag.string, seen_ids)
        if itemlist_jobs:
            jobs.extend(itemlist_jobs)

    if jobs:
        return _finalize_jobs(source, session, jobs, jsonld_jobs, timeout=timeout)

    jobs = _extract_jobs_from_links(source, soup, base_url)
    return _finalize_jobs(source, session, jobs, jsonld_jobs, timeout=timeout)
