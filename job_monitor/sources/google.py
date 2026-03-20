import json
import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text


BASE_URL = "https://www.google.com"
BATCHEXECUTE_URL = (
    "https://www.google.com/about/careers/applications/_/HiringCportalFrontendUi/data/batchexecute"
)
SIGNIN_URL_PATTERN = re.compile(
    r"(https://www\.google\.com/about/careers/applications/signin\?jobId=[^\"'\s<>]+|"
    r"/about/careers/applications/signin\?jobId=[^\"'\s<>]+)"
)

logger = logging.getLogger(__name__)


def _decode_google_escapes(value: str) -> str:
    return (
        value.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\u00253D", "%3D")
        .replace("\\u00253d", "%3d")
    )


def _extract_at_token(html: str) -> str:
    patterns = [
        r'SNlM0e["\\\']?\s*[:=]\s*["\\\']([^"\\\']+)',
        r'["\\\']at["\\\']\s*[:=]\s*["\\\'](AF[^"\\\']+)',
        r"[?&]at=([^&\"'\s]+)",
        r"(AF[a-zA-Z0-9_-]{10,}:[0-9]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return unescape(match.group(1))
    return ""


def _extract_tokens(html: str) -> Tuple[str, str, str]:
    f_sid = ""
    bl = ""
    at = _extract_at_token(html)

    def find_key(key: str) -> str:
        match = re.search(rf'{re.escape(key)}["\\\']?\s*[:=]\s*["\\\']([^"\\\']+)', html)
        return match.group(1) if match else ""

    f_sid = find_key("f.sid") or find_key("FdrFJe")
    at = at or find_key("SNlM0e")
    bl = find_key("bl") or find_key("cfb2h")

    if "WIZ_global_data" in html and (not f_sid or not bl or not at):
        match = re.search(r"WIZ_global_data\s*=\s*(\{.*?\})\s*;", html, re.S)
        if match:
            raw = match.group(1)
            data = None
            try:
                data = json.loads(raw)
            except Exception:
                data = None
            if isinstance(data, dict):
                f_sid = f_sid or data.get("FdrFJe") or data.get("f.sid", "")
                bl = bl or data.get("cfb2h") or data.get("bl", "")
                at = at or data.get("SNlM0e", "")
            else:
                f_sid = f_sid or find_key("FdrFJe")
                bl = bl or find_key("cfb2h")
                at = at or find_key("SNlM0e")

    if not f_sid or not bl:
        for match in re.finditer(r'https://www\.google\.com[^"\s]+batchexecute\?[^"\s]+', html):
            try:
                url = match.group(0)
                query = parse_qs(urlparse(url).query)
                if not f_sid and query.get("f.sid"):
                    f_sid = query["f.sid"][0]
                if not bl and query.get("bl"):
                    bl = query["bl"][0]
            except Exception:
                continue

    return f_sid, bl, at


def _extract_jobs_from_landing_html(source: dict, html: str) -> List[Job]:
    decoded = _decode_google_escapes(unescape(html))
    seen_urls = set()
    jobs: List[Job] = []

    for match in SIGNIN_URL_PATTERN.finditer(decoded):
        raw_url = match.group(0).rstrip("\\")
        url = ensure_absolute_url(BASE_URL, raw_url).rstrip('",\'')
        if url in seen_urls:
            continue

        parsed = parse_qs(urlparse(url).query)
        title = normalize_text((parsed.get("title") or [""])[0])
        if not title:
            continue

        location = normalize_text((parsed.get("location") or parsed.get("loc") or [""])[0])
        if location.upper() == "US":
            location = "United States"

        seen_urls.add(url)
        jobs.append(
            Job(
                job_id=hash_job_id(source["name"], url),
                source=source["name"],
                title=title,
                location=location,
                url=url,
                posted_at=None,
                raw={"source": "google_landing_html"},
            )
        )

    return jobs


def _build_request_payload(query: str, location: str, lang: str, page: int) -> Dict[str, str]:
    inner = [
        [
            query,
            None,
            None,
            None,
            lang,
            None,
            [[location]] if location else [],
            page,
            None,
            None,
            1,
            None,
            None,
            None,
            None,
            None,
            [2],
        ]
    ]
    payload = [[[ "r06xKb", json.dumps(inner), None, "3" ]]]
    return {"f.req": json.dumps(payload)}


def _parse_batchexecute(text: str) -> List:
    start = text.find("[")
    if start == -1:
        return []
    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(text[start:])
    return data


def _extract_jobs(data: List) -> Tuple[List[list], int, int]:
    for entry in data:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        if entry[0] != "wrb.fr" or entry[1] != "r06xKb":
            continue
        try:
            payload = json.loads(entry[2])
        except Exception:
            continue
        if not payload:
            return [], 0, 0
        jobs_block = payload[0] if len(payload) > 0 else []
        jobs: List[list] = []
        if isinstance(jobs_block, list):
            if jobs_block and isinstance(jobs_block[0], list) and jobs_block[0] and isinstance(jobs_block[0][0], list):
                jobs = jobs_block[0]
            elif jobs_block and isinstance(jobs_block[0], list):
                jobs = jobs_block
        total = payload[2] if len(payload) > 2 and isinstance(payload[2], int) else 0
        page_size = payload[3] if len(payload) > 3 and isinstance(payload[3], int) else 0
        return jobs, total, page_size
    return [], 0, 0


def _extract_location(locations) -> str:
    if not locations:
        return ""
    parts = []
    for loc in locations:
        if isinstance(loc, list) and loc:
            display = loc[0]
            if display:
                parts.append(display)
    return normalize_text("; ".join(parts))


def _extract_posted_at(entry: list) -> datetime | None:
    timestamps = []
    for item in entry:
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], (int, float))
            and isinstance(item[1], (int, float))
        ):
            seconds, nanos = item
            timestamps.append(seconds + nanos / 1_000_000_000)
    if not timestamps:
        return None
    return datetime.fromtimestamp(max(timestamps), tz=timezone.utc)


def _extract_url(value) -> str:
    if isinstance(value, str) and value:
        return ensure_absolute_url(BASE_URL, value)

    if isinstance(value, dict):
        for key in ("url", "jobUrl", "applyUrl"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return ensure_absolute_url(BASE_URL, candidate)
        return ""

    if isinstance(value, (list, tuple)):
        for item in value:
            url = _extract_url(item)
            if url:
                return url
        return ""

    return ""


def fetch_google_jobs(source: dict, session) -> List[Job]:
    max_pages = int(source.get("max_pages", 5))
    jobs: List[Job] = []

    parsed = urlparse(source["url"])
    query = parse_qs(parsed.query)
    search = (query.get("q") or [""])[0]
    location = (query.get("location") or [""])[0]
    lang = (query.get("hl") or ["en"])[0]

    landing = request_with_retries(session, "GET", source["url"], timeout=20)
    landing_jobs = _extract_jobs_from_landing_html(source, landing.text)
    f_sid, bl, at_token = _extract_tokens(landing.text)
    if not f_sid or not bl:
        if landing_jobs:
            logger.warning("Google token extraction failed; using landing-page fallback.")
            return landing_jobs
        raise ValueError("Unable to extract Google Careers tokens.")

    for page in range(1, max_pages + 1):
        params = {
            "rpcids": "r06xKb",
            "source-path": "/about/careers/applications/jobs/results/",
            "f.sid": f_sid,
            "bl": bl,
            "hl": lang,
            "soc-app": "1",
            "soc-platform": "1",
            "soc-device": "1",
            "_reqid": str(100000 + page),
            "rt": "c",
        }

        data = _build_request_payload(search, location, lang, page)
        if at_token:
            data["at"] = at_token

        headers = {
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "origin": BASE_URL,
            "x-same-domain": "1",
            "referer": BASE_URL,
        }

        try:
            resp = request_with_retries(
                session,
                "POST",
                BATCHEXECUTE_URL,
                params=params,
                data=data,
                headers=headers,
                timeout=20,
            )
        except requests.RequestException:
            if page == 1 and landing_jobs:
                logger.warning("Google batchexecute request failed; using landing-page fallback.")
                return landing_jobs
            raise
        resp.raise_for_status()

        payload = _parse_batchexecute(resp.text)
        job_entries, total, page_size = _extract_jobs(payload)
        if not job_entries:
            if page == 1 and landing_jobs:
                logger.warning("Google batchexecute returned no jobs; using landing-page fallback.")
                return landing_jobs
            if page == 1 and not at_token:
                raise ValueError("Unable to extract Google Careers tokens.")
            break

        for entry in job_entries:
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            title = normalize_text(entry[1])
            url = _extract_url(entry[2])
            if not title:
                parsed = parse_qs(urlparse(url).query)
                title = normalize_text((parsed.get("title") or [""])[0])
            if not title or not url:
                continue

            locations = entry[9] if len(entry) > 9 else []
            location_text = _extract_location(locations)
            posted_at = _extract_posted_at(entry)

            job_key = hash_job_id(source["name"], url)
            jobs.append(
                Job(
                    job_id=job_key,
                    source=source["name"],
                    title=title,
                    location=location_text,
                    url=url,
                    posted_at=posted_at,
                    raw=entry,
                )
            )

        if total and page_size and page * page_size >= total:
            break

    if jobs:
        return jobs
    if landing_jobs:
        logger.warning("Google API parsing returned no jobs; using landing-page fallback.")
        return landing_jobs
    raise ValueError("Google returned no job postings.")
