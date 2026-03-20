import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from dateutil import parser as date_parser


COMMON_POSTED_AT_KEYS = (
    "postedTs",
    "postedAt",
    "posted_at_text",
    "postedText",
    "posted_text",
    "datePosted",
    "dateText",
    "date_text",
    "postingDate",
    "createdAt",
    "creationTs",
    "publishedAt",
    "updatedAt",
    "indexedDate",
    "t_update",
    "t_create",
    "timestamp",
)

DATE_ONLY_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
    re.compile(r"^[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}$"),
)


def hash_job_id(source: str, url: str) -> str:
    payload = f"{source}::{url}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_text(text: Optional[Any]) -> str:
    if text is None:
        return ""

    if isinstance(text, (list, tuple, set)):
        pieces = [normalize_text(item) for item in text]
        text = "; ".join([piece for piece in pieces if piece])
    elif isinstance(text, dict):
        for key in ("title", "name", "label", "value", "text"):
            if key in text:
                text = text[key]
                break
        else:
            text = json.dumps(text, ensure_ascii=False, sort_keys=True)
    elif not isinstance(text, str):
        text = str(text)

    if not text:
        return ""

    return re.sub(r"\s+", " ", text).strip()


def parse_date(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            # Heuristic: treat large numbers as milliseconds
            if value > 10_000_000_000:
                value = value / 1000.0
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            dt = date_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    return None


def extract_posted_value(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return None
    for key in COMMON_POSTED_AT_KEYS:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def is_date_only_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if not cleaned:
        return False
    if ":" in cleaned or "T" in cleaned or "t" in cleaned:
        return False
    return any(pattern.fullmatch(cleaned) for pattern in DATE_ONLY_PATTERNS)


def is_date_only_posted_at(job) -> bool:
    raw_posted_value = extract_posted_value(getattr(job, "raw", None))
    if is_date_only_value(raw_posted_value):
        return True
    raw = getattr(job, "raw", None)
    return isinstance(raw, dict) and bool(raw.get("posted_at_is_date_only"))


def ensure_absolute_url(base_url: str, url: str) -> str:
    if not url:
        return url
    return urljoin(base_url, url)


def extract_jsonld_objects(raw_text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        payload = json.loads(raw_text)
    except Exception:
        return items

    def _walk(node: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from _walk(value)
        elif isinstance(node, list):
            for child in node:
                yield from _walk(child)

    for obj in _walk(payload):
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("@type")
        if obj_type == "JobPosting":
            items.append(obj)
        elif isinstance(obj_type, list) and "JobPosting" in obj_type:
            items.append(obj)

    return items
