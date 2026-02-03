import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from ..http_utils import request_with_retries
from ..models import Job
from ..utils import hash_job_id, normalize_text


BASE_URL = "https://www.google.com"
BATCHEXECUTE_URL = (
    "https://www.google.com/about/careers/applications/_/HiringCportalFrontendUi/data/batchexecute"
)


def _extract_tokens(html: str) -> Tuple[str, str, str]:
    f_sid = ""
    bl = ""
    at = ""

    match = re.search(r'"f.sid":\s*"?(\d+)"?', html)
    if match:
        f_sid = match.group(1)

    match = re.search(r'"SNlM0e":"([^"]+)"', html)
    if match:
        at = match.group(1)

    match = re.search(r'&bl=([\\w.-]+)', html)
    if match:
        bl = match.group(1)
    else:
        match = re.search(r'"bl":"([^"]+)"', html)
        if match:
            bl = match.group(1)

    return f_sid, bl, at


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
        jobs = jobs_block[0] if jobs_block and isinstance(jobs_block[0], list) else []
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


def fetch_google_jobs(source: dict, session) -> List[Job]:
    max_pages = int(source.get("max_pages", 5))
    jobs: List[Job] = []

    parsed = urlparse(source["url"])
    query = parse_qs(parsed.query)
    search = (query.get("q") or [""])[0]
    location = (query.get("location") or [""])[0]
    lang = (query.get("hl") or ["en"])[0]

    landing = request_with_retries(session, "GET", source["url"], timeout=20)
    f_sid, bl, at_token = _extract_tokens(landing.text)
    if not f_sid or not bl or not at_token:
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
        data["at"] = at_token

        headers = {
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "origin": BASE_URL,
            "x-same-domain": "1",
            "referer": BASE_URL,
        }

        resp = request_with_retries(
            session,
            "POST",
            BATCHEXECUTE_URL,
            params=params,
            data=data,
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()

        payload = _parse_batchexecute(resp.text)
        job_entries, total, page_size = _extract_jobs(payload)
        if not job_entries:
            break

        for entry in job_entries:
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            job_id = entry[0]
            title = normalize_text(entry[1])
            url = entry[2]
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

    return jobs
